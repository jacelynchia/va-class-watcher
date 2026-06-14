"""
Virgin Active class watcher (cloud edition)
===========================================
Watches one or more Virgin Active classes and sends you a Telegram message
the moment a spot opens up (SpacesRemaining goes above 0). Classes you are
already booked into are skipped automatically, so you only hear about ones
you still need.

Designed to run unattended on GitHub Actions, so it does NOT need your laptop.
It logs in by itself each run, so there is no token to copy by hand.

RECURRING CLASSES
-----------------
Each watchlist entry can use either:
  "weekday": "Monday"     -> auto-rolls to the next Monday, every run. Best for
                             a weekly recurring class. Set once, never touch it.
  "date": "2026-06-15"    -> a single fixed date (YYYY-MM-DD), for a one-off.
Weekdays are resolved in Singapore time, so "Monday" always means your Monday.

WHAT YOU CONFIGURE
------------------
1. The WATCHLIST below (which classes to watch). Add as many as you like.
2. Four secrets, set as environment variables (GitHub Secrets in the cloud):
      VA_USERNAME          your membership number (e.g. 110035581)
      VA_PASSWORD          your mylocker password
      TELEGRAM_BOT_TOKEN   from BotFather
      TELEGRAM_CHAT_ID     your chat id (a number)

HOW TO TEST (no need to wait for a real spot)
---------------------------------------------
Temporarily add a class that is OPEN right now to the WATCHLIST, run the bot
(or click "Run workflow" in GitHub Actions), and you should get an alert within
a minute. Then remove the test class and keep your real ones.
"""

import os
import sys
import time
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

# Singapore timezone, so weekday and date logic matches your local calendar.
SGT = ZoneInfo("Asia/Singapore")

# ============================================================
# WATCHLIST - edit this to track any classes you want
# ============================================================
# "match" uses case-insensitive "contains", so "BODYPUMP" matches "BODYPUMP(tm)"
# and "Grace" matches "Grace L.". Include just enough fields to be unambiguous.
#
# site:     the SiteID. "SPL" is Paya Lebar.
# weekday:  "Monday".."Sunday" for a recurring class (auto-rolls each week), OR
# date:     "YYYY-MM-DD" for a one-off. Use one or the other, not both.

WATCHLIST = [
    {
        "label": "BODYPUMP, Mon 8:15pm, Grace L. (Paya Lebar)",
        "site": "SPL",
        "weekday": "Monday",
        "match": {"ClassName": "BODYPUMP", "TimeString": "8:15pm", "Instructor": "Grace"},
    },
    {
        "label": "BODYPUMP, Wed 8:00pm, Grace L. (Paya Lebar)",
        "site": "SPL",
        "weekday": "Wednesday",
        "match": {"ClassName": "BODYPUMP", "TimeString": "8:00pm", "Instructor": "Grace"},
    },
]

# ============================================================
# RUN BEHAVIOUR
# ============================================================
POLL_INTERVAL_SECONDS = 60      # how often to check. 60 is gentle and plenty fast.
RUN_DURATION_MINUTES = 330     # ~5.5 hours of continuous polling per run.
REQUEST_TIMEOUT = 30            # seconds to wait for the server before giving up.
LOGIN_MAX_ATTEMPTS = 3          # how many times to retry login on a network blip.

# ============================================================
# ENDPOINTS
# ============================================================
TOKEN_URL = "https://hal.virginactive.com.sg/token"
API_URL = "https://hal.virginactive.com.sg/api/classes/bookableclassquery"
BOOKINGS_URL = "https://hal.virginactive.com.sg/api/bookings/getbookings"

# ============================================================
# Secrets, read from the environment
# ============================================================
VA_USERNAME = os.environ.get("VA_USERNAME")
VA_PASSWORD = os.environ.get("VA_PASSWORD")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def log(msg):
    stamp = datetime.now(SGT).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def next_weekday_iso(weekday_name):
    """Return the ISO date of the next given weekday (today counts), in SGT."""
    key = weekday_name.strip().lower()
    if key not in WEEKDAYS:
        raise SystemExit(f"Unknown weekday '{weekday_name}'. Use Monday..Sunday.")
    today = datetime.now(SGT).date()
    days_ahead = (WEEKDAYS[key] - today.weekday()) % 7
    return (today + timedelta(days=days_ahead)).isoformat()


def resolve_date(entry):
    """Work out the date this entry should query: weekday auto-roll or fixed date."""
    if "weekday" in entry:
        return next_weekday_iso(entry["weekday"])
    if "date" in entry:
        return entry["date"]
    raise SystemExit(f"Entry '{entry.get('label')}' needs a 'weekday' or a 'date'.")


def send_telegram(text):
    """Send a message to your phone via the Telegram bot."""
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


def login():
    """
    Log in and return a fresh Bearer token.

    Uses the ASP.NET OAuth password flow (confirmed against the token request).
    Retries a few times so a single slow response does not kill the run.
    """
    if not VA_USERNAME or not VA_PASSWORD:
        raise SystemExit("Missing VA_USERNAME or VA_PASSWORD. Set them as secrets.")

    data = {
        "grant_type": "password",
        "username": VA_USERNAME,
        "password": VA_PASSWORD,
    }

    last_error = None
    for attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
        try:
            r = requests.post(TOKEN_URL, data=data, timeout=REQUEST_TIMEOUT)
            if r.status_code == 400:
                raise SystemExit(
                    "Login was rejected (400). Double check your username and "
                    "password secrets, and confirm the token request body matches."
                )
            r.raise_for_status()
            token = r.json().get("access_token")
            if not token:
                raise SystemExit(
                    f"Login succeeded but no access_token in response: {r.text[:200]}"
                )
            log("Logged in, got a fresh token.")
            return token
        except requests.RequestException as e:
            last_error = e
            log(f"Login attempt {attempt} of {LOGIN_MAX_ATTEMPTS} failed ({e}).")
            if attempt < LOGIN_MAX_ATTEMPTS:
                time.sleep(5)

    raise SystemExit(f"Login failed after {LOGIN_MAX_ATTEMPTS} attempts: {last_error}")


def get_my_booked_ids(token):
    """
    Return a set of BookingIDs the member is already booked into.

    Used to skip alerting for classes you have already secured. Returns an
    empty set on any error, so a hiccup here never blocks the normal watching.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://mylocker.virginactive.com.sg",
        "Referer": "https://mylocker.virginactive.com.sg/",
        "X-Mylocker-Language": "en-SG",
    }
    try:
        r = requests.get(BOOKINGS_URL, headers=headers, timeout=REQUEST_TIMEOUT)
        if r.status_code == 401:
            raise PermissionError("token expired")
        r.raise_for_status()
        data = r.json()
        bookings = data.get("MyBookings", {}).get("Bookings", []) or []
        return {b.get("BookingID") for b in bookings if b.get("BookingID") is not None}
    except PermissionError:
        raise
    except requests.RequestException as e:
        log(f"Could not fetch your bookings ({e}). Will not skip any this cycle.")
        return set()


def query_classes(token, site, iso_date):
    """Fetch the class list for one club and one date."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://mylocker.virginactive.com.sg",
        "Referer": "https://mylocker.virginactive.com.sg/",
        "X-Mylocker-Language": "en-SG",
    }
    payload = {"Category": 0, "AMPM": "ALL", "ISODate": iso_date, "SiteID": site}
    r = requests.post(API_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    if r.status_code == 401:
        raise PermissionError("token expired")
    r.raise_for_status()
    return r.json()


def class_matches(cls, criteria):
    """True if every criterion is a case-insensitive substring of the class field."""
    for field, wanted in criteria.items():
        actual = str(cls.get(field, "")).lower()
        if wanted.lower() not in actual:
            return False
    return True


def main():
    if "--test-telegram" in sys.argv:
        log("Sending a Telegram test message...")
        send_telegram("Test from your Virgin Active watcher. Telegram is wired up.")
        log("Done. Check your phone.")
        return

    if not WATCHLIST:
        raise SystemExit("WATCHLIST is empty. Add at least one class to watch.")

    # Resolve each entry's date now (weekday auto-roll), then group by (site, date)
    # so we make one API call per unique club and date.
    groups = {}
    for entry in WATCHLIST:
        iso_date = resolve_date(entry)
        log(f"Watching '{entry['label']}' on {iso_date}.")
        groups.setdefault((entry["site"], iso_date), []).append(entry)

    token = login()
    alerted = set()  # labels already alerted this run, to avoid repeat pings
    stop_at = datetime.now() + timedelta(minutes=RUN_DURATION_MINUTES)

    log(f"This run lasts {RUN_DURATION_MINUTES} min, checking every {POLL_INTERVAL_SECONDS}s.")

    while datetime.now() < stop_at:
        # Feature 1: fetch the classes you are already booked into, so we can
        # skip alerting for those. Refreshed every cycle so it stays current
        # (e.g. if you book one mid-run, it goes quiet on the next pass).
        try:
            booked_ids = get_my_booked_ids(token)
        except PermissionError:
            log("Token expired, logging in again...")
            token = login()
            booked_ids = get_my_booked_ids(token)

        for (site, iso_date), entries in groups.items():
            try:
                classes = query_classes(token, site, iso_date)
            except PermissionError:
                log("Token expired, logging in again...")
                token = login()
                classes = query_classes(token, site, iso_date)
            except requests.RequestException as e:
                log(f"Network issue on {site} {iso_date}: {e}. Retrying next cycle.")
                continue

            for entry in entries:
                label = entry["label"]
                hit = next((c for c in classes if class_matches(c, entry["match"])), None)

                if hit is None:
                    log(f"'{label}': not found in results (check match fields).")
                    continue

                # Feature 1: already booked? Stay quiet.
                if hit.get("BookingID") in booked_ids:
                    log(f"'{label}': already booked, skipping.")
                    continue

                spaces = hit.get("SpacesRemaining", 0)
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

        time.sleep(POLL_INTERVAL_SECONDS)

    log("Run finished. The schedule will start the next one shortly.")


if __name__ == "__main__":
    main()
