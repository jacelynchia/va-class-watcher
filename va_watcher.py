"""
Virgin Active class watcher (cloud edition, interactive)
========================================================
Watches Virgin Active classes and sends a Telegram message the moment a spot
opens up. Classes you are already booked into are skipped automatically.

You can control it by texting the bot on Telegram:
  /watch class=BODYPUMP day=Friday time=7:00pm instructor=Grace [site=SPL]
  /unwatch 2          (remove item 2 from the list)
  /list               (show what is being watched)
  /status             (is it running, what is watched)
  /help               (show command help)

Runs on GitHub Actions. While a run is active it replies to commands within a
second or two (Telegram long-polling). Between runs there can be a short delay.

Persistence: the live watchlist is stored in state.json in this repo, updated
by the bot. This needs the repo's Actions workflow permission set to read/write
(Settings > Actions > General > Workflow permissions).
"""

import os
import sys
import json
import time
import base64
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")

# ============================================================
# DEFAULT WATCHLIST (used to seed state.json on the very first run)
# ============================================================
# After the first run, the live list lives in state.json and is edited via
# Telegram commands. Editing here only matters before state.json exists.
DEFAULT_WATCHLIST = [
    {
        "site": "SPL",
        "weekday": "Monday",
        "match": {"ClassName": "BODYPUMP", "TimeString": "8:15pm", "Instructor": "Grace"},
    },
    {
        "site": "SPL",
        "weekday": "Wednesday",
        "match": {"ClassName": "BODYPUMP", "TimeString": "8:00pm", "Instructor": "Grace"},
    },
]

# ============================================================
# RUN BEHAVIOUR
# ============================================================
POLL_INTERVAL_SECONDS = 60      # how often to check availability.
RUN_DURATION_MINUTES = 230      # ~3h50m of polling per run (see workflow schedule).
REQUEST_TIMEOUT = 30            # seconds for normal HTTP calls.
LOGIN_MAX_ATTEMPTS = 3          # login retries on a network blip.
DEFAULT_SITE = "SPL"            # used when a /watch command omits site.
BURST_LEAD_SECONDS = 45        # start hammering this many secs before a known open-time.
BURST_POLL_SECONDS = 1.5       # poll interval while in burst/scramble mode.
BOOK_OPEN_DAYS_BEFORE = 7      # booking opens this many days before the class...
BOOK_OPEN_HOUR = 21            # ...at this hour (SGT), i.e. 9pm.
MAX_SEAT_ATTEMPTS = 25         # how many seats to try before giving up one pass.

# All Virgin Active Singapore clubs, by SiteID, with friendly names and aliases.
# Captured from the getoptions response. Add new clubs here if they open.
CLUBS = {
    "SRP": {"name": "Raffles Place", "aliases": ["raffles place", "raffles", "rp"]},
    "STP": {"name": "Tanjong Pagar", "aliases": ["tanjong pagar", "tanjong", "tp"]},
    "SHV": {"name": "Holland Village", "aliases": ["holland village", "holland", "hv"]},
    "SMO": {"name": "Marina One", "aliases": ["marina one", "marina", "mo"]},
    "SPL": {"name": "Paya Lebar", "aliases": ["paya lebar", "paya", "pl"]},
}


def resolve_site(value):
    """
    Turn a club name, alias, or code into a SiteID. Returns the SiteID string,
    or None if it cannot be matched. Case-insensitive and quote-tolerant.
    """
    if not value:
        return DEFAULT_SITE
    v = value.strip().strip('"').strip("'").lower()
    # direct code match (spl, smo, ...)
    for code in CLUBS:
        if v == code.lower():
            return code
    # name / alias match
    for code, info in CLUBS.items():
        if v == info["name"].lower() or v in info["aliases"]:
            return code
    # loose contains match (e.g. "marina one studio")
    for code, info in CLUBS.items():
        if info["name"].lower() in v or any(a in v for a in info["aliases"]):
            return code
    return None


def club_name(site):
    info = CLUBS.get(site)
    return info["name"] if info else site

# ============================================================
# ENDPOINTS
# ============================================================
TOKEN_URL = "https://hal.virginactive.com.sg/token"
API_URL = "https://hal.virginactive.com.sg/api/classes/bookableclassquery"
BOOKINGS_URL = "https://hal.virginactive.com.sg/api/bookings/getbookings"
CLASSOPTIONS_URL = "https://hal.virginactive.com.sg/api/classes/getclassoptions"
MAKEBOOKING_URL = "https://hal.virginactive.com.sg/api/bookings/makeclassbooking"

# ============================================================
# Secrets / environment
# ============================================================
VA_USERNAME = os.environ.get("VA_USERNAME")
VA_PASSWORD = os.environ.get("VA_PASSWORD")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GH_TOKEN = os.environ.get("GH_TOKEN")              # auto GITHUB_TOKEN, passed in workflow
GH_REPO = os.environ.get("GITHUB_REPOSITORY")      # auto-set by Actions, e.g. owner/repo
STATE_PATH = "state.json"

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

RUN_STOP_AT = None    # set in main(), used by /status
STATE_SHA = None      # GitHub file sha for state.json, used to update it


def log(msg):
    stamp = datetime.now(SGT).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


# ------------------------------------------------------------
# Dates
# ------------------------------------------------------------
def next_weekday_iso(weekday_name):
    key = weekday_name.strip().lower()
    if key not in WEEKDAYS:
        raise ValueError(f"Unknown weekday '{weekday_name}'.")
    today = datetime.now(SGT).date()
    days_ahead = (WEEKDAYS[key] - today.weekday()) % 7
    return (today + timedelta(days=days_ahead)).isoformat()


def resolve_date(entry):
    if "weekday" in entry:
        return next_weekday_iso(entry["weekday"])
    if "date" in entry:
        return entry["date"]
    raise ValueError("Entry needs a 'weekday' or a 'date'.")


def entry_label(entry):
    m = entry.get("match", {})
    when = entry.get("weekday") or entry.get("date", "")
    s = f"{m.get('ClassName', 'class')}, {when} {m.get('TimeString', '')}".strip()
    if m.get("Instructor"):
        s += f", {m['Instructor']}"
    if entry.get("site"):
        s += f" ({club_name(entry['site'])})"
    return s


# ------------------------------------------------------------
# Telegram
# ------------------------------------------------------------
def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram not configured (missing bot token or chat id).")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log(f"Failed to send Telegram message: {e}")


def tg_get_updates(offset, long_poll=True):
    """Fetch new messages. long_poll blocks up to ~50s; otherwise returns fast."""
    if not TELEGRAM_BOT_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    timeout = 50 if long_poll else 0
    r = requests.get(url, params={"offset": offset, "timeout": timeout},
                     timeout=(70 if long_poll else REQUEST_TIMEOUT))
    r.raise_for_status()
    return r.json().get("result", [])


# ------------------------------------------------------------
# State persistence (state.json committed to the repo via the GitHub API)
# ------------------------------------------------------------
def _gh_headers():
    return {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}


def gh_read_state():
    """Return (state_dict, sha) or (None, None) if missing/unavailable."""
    if not GH_TOKEN or not GH_REPO:
        return None, None
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{STATE_PATH}"
    r = requests.get(url, headers=_gh_headers(), params={"ref": "main"}, timeout=REQUEST_TIMEOUT)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    j = r.json()
    content = base64.b64decode(j["content"]).decode("utf-8")
    return json.loads(content), j["sha"]


def gh_write_state(payload, sha):
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{STATE_PATH}"
    body = {
        "message": "update watcher state",
        "content": base64.b64encode(json.dumps(payload, indent=2).encode()).decode(),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=_gh_headers(), json=body, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()["content"]["sha"]


def load_state():
    global STATE_SHA
    data, sha = None, None
    try:
        data, sha = gh_read_state()
    except Exception as e:
        log(f"Could not read saved state ({e}). Falling back to defaults.")
    STATE_SHA = sha
    if not data:
        data = {"watchlist": list(DEFAULT_WATCHLIST), "tg_offset": 0}
    data.setdefault("watchlist", [])
    data.setdefault("tg_offset", 0)
    return data


def save_state(state):
    global STATE_SHA
    if not GH_TOKEN or not GH_REPO:
        return  # cannot persist; changes live only for this run
    payload = {"watchlist": state["watchlist"], "tg_offset": state["tg_offset"]}
    try:
        STATE_SHA = gh_write_state(payload, STATE_SHA)
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        if status in (409, 422):
            try:
                _, STATE_SHA = gh_read_state()
                STATE_SHA = gh_write_state(payload, STATE_SHA)
            except Exception as e2:
                log(f"Could not save state after conflict ({e2}).")
        else:
            log(f"Could not save state ({e}).")
    except Exception as e:
        log(f"Could not save state ({e}).")


# ------------------------------------------------------------
# Virgin Active API
# ------------------------------------------------------------
def login():
    if not VA_USERNAME or not VA_PASSWORD:
        raise SystemExit("Missing VA_USERNAME or VA_PASSWORD. Set them as secrets.")
    data = {"grant_type": "password", "username": VA_USERNAME, "password": VA_PASSWORD}
    last_error = None
    for attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
        try:
            r = requests.post(TOKEN_URL, data=data, timeout=REQUEST_TIMEOUT)
            if r.status_code == 400:
                raise SystemExit("Login rejected (400). Check VA_USERNAME / VA_PASSWORD.")
            r.raise_for_status()
            token = r.json().get("access_token")
            if not token:
                raise SystemExit(f"Login gave no access_token: {r.text[:200]}")
            log("Logged in, got a fresh token.")
            return token
        except requests.RequestException as e:
            last_error = e
            log(f"Login attempt {attempt} of {LOGIN_MAX_ATTEMPTS} failed ({e}).")
            if attempt < LOGIN_MAX_ATTEMPTS:
                time.sleep(5)
    raise SystemExit(f"Login failed after {LOGIN_MAX_ATTEMPTS} attempts: {last_error}")


def _va_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://mylocker.virginactive.com.sg",
        "Referer": "https://mylocker.virginactive.com.sg/",
        "X-Mylocker-Language": "en-SG",
    }


def get_my_booked_ids(token):
    try:
        r = requests.get(BOOKINGS_URL, headers=_va_headers(token), timeout=REQUEST_TIMEOUT)
        if r.status_code == 401:
            raise PermissionError("token expired")
        r.raise_for_status()
        bookings = (r.json().get("MyBookings", {}) or {}).get("Bookings", []) or []
        return {b.get("BookingID") for b in bookings if b.get("BookingID") is not None}
    except PermissionError:
        raise
    except requests.RequestException as e:
        log(f"Could not fetch bookings ({e}). Not skipping any this cycle.")
        return set()


def query_classes(token, site, iso_date):
    headers = dict(_va_headers(token))
    headers["Content-Type"] = "application/json;charset=UTF-8"
    payload = {"Category": 0, "AMPM": "ALL", "ISODate": iso_date, "SiteID": site}
    r = requests.post(API_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    if r.status_code == 401:
        raise PermissionError("token expired")
    r.raise_for_status()
    return r.json()


def class_matches(cls, criteria):
    for field, wanted in criteria.items():
        if wanted.lower() not in str(cls.get(field, "")).lower():
            return False
    return True


# ------------------------------------------------------------
# Availability check
# ------------------------------------------------------------
def compute_open_datetime(iso_date):
    """Booking opens BOOK_OPEN_DAYS_BEFORE days before the class, at BOOK_OPEN_HOUR SGT."""
    class_day = datetime.strptime(iso_date, "%Y-%m-%d").date()
    open_day = class_day - timedelta(days=BOOK_OPEN_DAYS_BEFORE)
    return datetime(open_day.year, open_day.month, open_day.day,
                    BOOK_OPEN_HOUR, 0, 0, tzinfo=SGT)


def is_hot_window(entries):
    """
    True if any armed (autobook) entry is in an aggressive-polling window:
    either near its known booking-open time, or already past it (scramble for
    cancellations). Non-armed entries never make it hot.
    """
    now = datetime.now(SGT)
    for e in entries:
        if not e.get("autobook"):
            continue
        try:
            open_dt = compute_open_datetime(resolve_date(e))
        except Exception:
            return True  # armed but can't compute time -> be safe, hammer
        # Hot from BURST_LEAD_SECONDS before the open time onward (scramble after).
        if now >= open_dt - timedelta(seconds=BURST_LEAD_SECONDS):
            return True
    return False


def get_free_seats(token, booking_id, plus2id):
    """
    Return a list of available SeatNumbers for a class, best first.
    A seat is free when RoomItemType == 1. Returns [] on any problem.
    """
    headers = dict(_va_headers(token))
    headers["Content-Type"] = "application/json;charset=UTF-8"
    payload = {"BookingID": booking_id, "Plus2DescriptionProductID": plus2id}
    r = requests.post(CLASSOPTIONS_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    if r.status_code == 401:
        raise PermissionError("token expired")
    r.raise_for_status()
    data = r.json()
    free = []
    for row in data.get("RoomLayout", []) or []:
        for cell in row:
            if cell.get("RoomItemType") == 1 and cell.get("SeatNumber", 0) > 0:
                free.append(cell["SeatNumber"])
    return free


def make_booking(token, booking_id, seat_number):
    """
    Attempt one booking of a specific seat. Returns (ok, message).
    ok is True only when the API confirms Success == true.
    """
    headers = dict(_va_headers(token))
    headers["Content-Type"] = "application/json;charset=UTF-8"
    payload = {
        "MemberID": int(VA_USERNAME),
        "BookingID": booking_id,
        "SeatNumber": seat_number,
        "Message": "",
    }
    r = requests.post(MAKEBOOKING_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    if r.status_code == 401:
        raise PermissionError("token expired")
    r.raise_for_status()
    data = r.json()
    if data.get("Success") is True:
        return True, "booked"
    return False, (data.get("ErrorMessage") or "unknown error")


def try_autobook(token, hit, label):
    """
    Try hard to book the matched class. Reads free seats, books the first,
    retries through other seats if one is snatched. Returns (booked, seat_or_msg).
    """
    booking_id = hit.get("BookingID")
    plus2id = hit.get("Plus2Identifier")
    if not booking_id or not plus2id:
        return False, "missing booking id / plus2 id"

    attempts = 0
    while attempts < MAX_SEAT_ATTEMPTS:
        try:
            free = get_free_seats(token, booking_id, plus2id)
        except PermissionError:
            raise
        except requests.RequestException as e:
            return False, f"seat lookup failed: {e}"

        if not free:
            return False, "no free seats"

        seat = free[0]
        attempts += 1
        try:
            ok, msg = make_booking(token, booking_id, seat)
        except PermissionError:
            raise
        except requests.RequestException as e:
            return False, f"booking call failed: {e}"

        if ok:
            return True, seat
        # seat was probably taken between read and book -> loop and try another
        log(f"'{label}': seat {seat} failed ({msg}), retrying...")

    return False, "exhausted seat attempts"


def check_availability(token, state, alerted):
    """
    One availability pass over all watched entries. Armed entries (autobook)
    are booked automatically; others just alert. Returns True if any armed
    entry was booked (so the caller can persist the disarmed state).
    """
    entries = state["watchlist"]
    if not entries:
        return False

    groups = {}
    for e in entries:
        try:
            d = resolve_date(e)
        except ValueError as ex:
            log(f"Bad entry skipped ({ex}).")
            continue
        groups.setdefault((e["site"], d), []).append(e)

    booked_ids = get_my_booked_ids(token)  # may raise PermissionError
    changed = False

    for (site, iso_date), es in groups.items():
        try:
            classes = query_classes(token, site, iso_date)
        except requests.RequestException as ex:
            log(f"Network issue on {site} {iso_date}: {ex}. Skipping this cycle.")
            continue
        for e in es:
            label = entry_label(e)
            hit = next((c for c in classes if class_matches(c, e["match"])), None)
            if hit is None:
                log(f"'{label}': not found in results.")
                continue
            if hit.get("BookingID") in booked_ids:
                log(f"'{label}': already booked, skipping.")
                if e.get("autobook"):
                    # Already in it, so the arm is done.
                    e.pop("autobook", None)
                    changed = True
                continue

            spaces = hit.get("SpacesRemaining", 0)

            # ---- ARMED: auto-book ----
            if e.get("autobook"):
                if spaces > 0:
                    log(f"'{label}': ARMED and open ({spaces}). Attempting to book...")
                    try:
                        ok, info = try_autobook(token, hit, label)
                    except PermissionError:
                        raise
                    if ok:
                        log(f"'{label}': BOOKED, seat {info}. Disarming.")
                        send_telegram(
                            f"<b>Booked!</b>\n{label}\nSeat {info}. "
                            f"Auto-book done, this class is now disarmed."
                        )
                        e.pop("autobook", None)
                        changed = True
                    else:
                        log(f"'{label}': book attempt failed ({info}).")
                        # stay armed, try again next cycle
                else:
                    log(f"'{label}': armed, still full.")
                continue

            # ---- NOT armed: alert only ----
            if spaces > 0:
                if label not in alerted:
                    log(f"'{label}': SPOT OPEN ({spaces} left). Alerting!")
                    send_telegram(
                        f"<b>Spot available!</b>\n{label}\n"
                        f"{spaces} space(s) just opened.\n"
                        f"Book now: https://mylocker.virginactive.com.sg/#/bookaclass"
                    )
                    alerted.add(label)
                else:
                    log(f"'{label}': still open, already alerted this run.")
            else:
                log(f"'{label}': full.")

    return changed


# ------------------------------------------------------------
# Telegram commands
# ------------------------------------------------------------
HELP_TEXT = (
    "<b>Virgin Active watcher commands</b>\n"
    "/watch class=BODYPUMP day=Friday time=7:00pm instructor=Grace club=\"Marina One\"\n"
    "   (club is optional, defaults to Paya Lebar; instructor is optional)\n"
    "/unwatch N    remove item N from the list\n"
    "/list    show what is being watched\n"
    "/clubs    list the clubs you can watch\n"
    "/autobook N    auto-book item N the moment booking opens (spends a credit!)\n"
    "/cancelbook N    turn off auto-book for item N (keep watching)\n"
    "/status    is it running, what is watched\n"
    "/help    this message\n\n"
    "Tip: use single words for values, e.g. class=BODYPUMP, instructor=Grace, "
    "and the exact time shown in the app, e.g. time=7:00pm."
)


def parse_kv(args_text):
    out = {}
    for tok in args_text.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def cmd_list(state):
    entries = state["watchlist"]
    if not entries:
        send_telegram("Not watching anything right now. Add one with /watch.")
        return
    lines = ["<b>Currently watching:</b>"]
    for i, e in enumerate(entries, 1):
        try:
            when = resolve_date(e)
        except ValueError:
            when = "?"
        tag = ""
        if e.get("autobook"):
            try:
                opent = compute_open_datetime(when).strftime("%a %d %b %H:%M")
                tag = f"  [AUTO-BOOK, opens {opent}]"
            except Exception:
                tag = "  [AUTO-BOOK]"
        lines.append(f"{i}. {entry_label(e)}  (next: {when}){tag}")
    send_telegram("\n".join(lines))


def cmd_status(state):
    n = len(state["watchlist"])
    if RUN_STOP_AT:
        ends = RUN_STOP_AT.astimezone(SGT).strftime("%H:%M")
        running = f"Active this run until about {ends} SGT, then the next run takes over."
    else:
        running = "Running."
    send_telegram(f"<b>Status</b>\n{running}\nWatching {n} class(es). Use /list to see them.")


def cmd_watch(args_text, state):
    kv = parse_kv(args_text)
    classname = kv.get("class") or kv.get("name")
    day = kv.get("day") or kv.get("weekday")
    one_date = kv.get("date")
    t = kv.get("time")
    instr = kv.get("instructor") or kv.get("coach")
    site_input = kv.get("club") or kv.get("site")
    site = resolve_site(site_input)
    if site is None:
        names = ", ".join(info["name"] for info in CLUBS.values())
        send_telegram(f"I don't recognise that club. Options: {names}.")
        return

    if not classname or not t or not (day or one_date):
        send_telegram(
            "I need at least class, time, and a day. Example:\n"
            "/watch class=BODYPUMP day=Friday time=7:00pm instructor=Grace"
        )
        return

    match = {"ClassName": classname, "TimeString": t.lower().replace(" ", "")}
    if instr:
        match["Instructor"] = instr
    entry = {"site": site, "match": match}
    if day:
        if day.strip().lower() not in WEEKDAYS:
            send_telegram(f"'{day}' is not a weekday. Use Monday..Sunday.")
            return
        entry["weekday"] = day.strip().capitalize()
    else:
        entry["date"] = one_date

    # Avoid duplicates (so reprocessing a command is harmless).
    for e in state["watchlist"]:
        if e.get("site") == entry["site"] and e.get("match") == entry["match"] and \
           e.get("weekday") == entry.get("weekday") and e.get("date") == entry.get("date"):
            send_telegram(f"Already watching: {entry_label(entry)}")
            return

    state["watchlist"].append(entry)
    send_telegram(f"Now watching: {entry_label(entry)}")


def cmd_unwatch(args_text, state):
    entries = state["watchlist"]
    arg = args_text.strip()
    if not arg.isdigit():
        send_telegram("Tell me which number to remove, e.g. /unwatch 2. Use /list to see numbers.")
        return
    idx = int(arg)
    if idx < 1 or idx > len(entries):
        send_telegram(f"There is no item {idx}. Use /list to see the current numbers.")
        return
    removed = entries.pop(idx - 1)
    send_telegram(f"Stopped watching: {entry_label(removed)}")


def cmd_clubs():
    lines = ["<b>Clubs you can watch:</b>"]
    for info in CLUBS.values():
        lines.append(f"- {info['name']}")
    lines.append("\nUse the name in /watch, e.g. club=\"Marina One\" (or just club=marina).")
    send_telegram("\n".join(lines))


def cmd_autobook(args_text, state):
    entries = state["watchlist"]
    arg = args_text.strip()
    if not arg.isdigit():
        send_telegram("Tell me which item to auto-book, e.g. /autobook 2. Use /list for numbers.")
        return
    idx = int(arg)
    if idx < 1 or idx > len(entries):
        send_telegram(f"There is no item {idx}. Use /list to see numbers.")
        return
    e = entries[idx - 1]
    e["autobook"] = True
    try:
        when = resolve_date(e)
        opent = compute_open_datetime(when).strftime("%a %d %b %H:%M")
        send_telegram(
            f"Armed for auto-book: {entry_label(e)}\n"
            f"Booking opens {opent} SGT. I'll grab a seat the moment it opens, "
            f"and keep trying for cancellations if I miss it. Disarms after booking."
        )
    except Exception:
        send_telegram(f"Armed for auto-book: {entry_label(e)}")


def cmd_cancelbook(args_text, state):
    entries = state["watchlist"]
    arg = args_text.strip()
    if not arg.isdigit():
        send_telegram("Tell me which item to disarm, e.g. /cancelbook 2.")
        return
    idx = int(arg)
    if idx < 1 or idx > len(entries):
        send_telegram(f"There is no item {idx}. Use /list to see numbers.")
        return
    e = entries[idx - 1]
    if e.pop("autobook", None):
        send_telegram(f"Auto-book turned off for: {entry_label(e)} (still watching).")
    else:
        send_telegram(f"That one wasn't armed anyway: {entry_label(e)}")


def handle_command(text, state):
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower().lstrip("/")
    args = parts[1] if len(parts) > 1 else ""
    # allow "/watch@botname" style
    cmd = cmd.split("@", 1)[0]

    if cmd in ("start", "help"):
        send_telegram(HELP_TEXT)
    elif cmd == "list":
        cmd_list(state)
    elif cmd == "status":
        cmd_status(state)
    elif cmd == "watch":
        cmd_watch(args, state)
    elif cmd == "unwatch":
        cmd_unwatch(args, state)
    elif cmd == "clubs":
        cmd_clubs()
    elif cmd == "autobook":
        cmd_autobook(args, state)
    elif cmd in ("cancelbook", "manual"):
        cmd_cancelbook(args, state)
    else:
        send_telegram("Unknown command. Send /help to see what I can do.")


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main():
    global RUN_STOP_AT

    if "--test-telegram" in sys.argv:
        log("Sending a Telegram test message...")
        send_telegram("Test from your Virgin Active watcher. Telegram is wired up.")
        log("Done. Check your phone.")
        return

    state = load_state()
    token = login()
    alerted = set()
    last_avail = 0.0
    RUN_STOP_AT = datetime.now() + timedelta(minutes=RUN_DURATION_MINUTES)

    log(f"Loaded {len(state['watchlist'])} watched class(es). "
        f"Run lasts {RUN_DURATION_MINUTES} min.")
    for e in state["watchlist"]:
        try:
            log(f"  - {entry_label(e)} (next: {resolve_date(e)})")
        except ValueError:
            log(f"  - {entry_label(e)} (bad date)")

    while datetime.now() < RUN_STOP_AT:
        hot = is_hot_window(state["watchlist"])
        avail_interval = BURST_POLL_SECONDS if hot else POLL_INTERVAL_SECONDS

        # 1. Commands. In relaxed mode we long-poll (which also paces the loop).
        #    In hot mode we do a quick non-blocking check so we can hammer bookings.
        try:
            updates = tg_get_updates(state["tg_offset"], long_poll=not hot)
            cmd_changed = False
            for u in updates:
                state["tg_offset"] = u["update_id"] + 1
                cmd_changed = True
                msg = u.get("message") or u.get("edited_message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue  # ignore anyone who is not you
                text = (msg.get("text") or "").strip()
                if text.startswith("/"):
                    try:
                        handle_command(text, state)
                    except Exception as ce:
                        log(f"Command error: {ce}")
                        send_telegram("Sorry, that command hit an error. Try /help.")
            if cmd_changed:
                save_state(state)
        except requests.RequestException as e:
            log(f"Telegram poll issue: {e}")
            time.sleep(2)

        # 2. Availability + auto-book, gated by the current interval.
        if time.time() - last_avail >= avail_interval:
            last_avail = time.time()
            try:
                booked_changed = check_availability(token, state, alerted)
                if booked_changed:
                    save_state(state)  # persist disarm after a successful booking
            except PermissionError:
                log("Token expired, logging in again...")
                token = login()

        # Pace the loop. Long-poll already paced us in relaxed mode; in hot mode
        # (or when Telegram is off) we sleep the short burst interval.
        if hot or not TELEGRAM_BOT_TOKEN:
            time.sleep(BURST_POLL_SECONDS if hot else POLL_INTERVAL_SECONDS)

    log("Run finished. The schedule will start the next one shortly.")


if __name__ == "__main__":
    main()
