"""
Virgin Active class watcher (cloud edition)
===========================================
Watches one or more Virgin Active classes and sends you a Telegram message
the moment a spot opens up (SpacesRemaining goes above 0).

Designed to run unattended on GitHub Actions, so it does NOT need your laptop.
It logs in by itself each run, so there is no token to copy by hand.

WHAT YOU CONFIGURE
------------------
1. The WATCHLIST below (which classes to watch). Add as many as you like.
2. Four secrets, set as environment variables (GitHub Secrets in the cloud):
      VA_USERNAME          your membership number (e.g. 110035581)
      VA_PASSWORD          your mylocker password
      TELEGRAM_BOT_TOKEN   from BotFather (see README)
      TELEGRAM_CHAT_ID     your chat id (see README)

HOW TO TEST (no need to wait for a real spot)
---------------------------------------------
A. Telegram plumbing only:   python va_watcher.py --test-telegram
   Sends a single test message. If it lands on your phone, Telegram is wired up.

B. Full pipeline:  temporarily add a class that is OPEN right now to the
   WATCHLIST (pick any class in the app that has space), then run the bot
   (or click "Run workflow" in GitHub Actions). You should get an alert
   within a minute. That proves login, query, matching, and Telegram all
   work. Then remove the test class and keep your real one.
"""

import os
import sys
import time
import requests
from datetime import datetime, timedelta

# ============================================================
# WATCHLIST - edit this to track any classes you want
# ============================================================
# Each entry watches one class on one date at one club.
# "match" uses case-insensitive "contains", so "BODYPUMP" matches "BODYPUMP(tm)"
# and "Grace" matches "Grace L.". Include just enough fields to be unambiguous.
#
# site:  the SiteID. "SPL" is Paya Lebar. (Capture other clubs the same way
#        we found SPL, from the request Payload.)
# date:  the ISODate string, format YYYY-MM-DD. Update weekly for recurring classes.

WATCHLIST = [
    {
        "label": "BODYPUMP, Mon 8:15pm, Grace L. (Paya Lebar)",
        "site": "SPL",
        "date": "2026-06-15",
        "match": {"ClassName": "BODYPUMP", "TimeString": "8:15pm", "Instructor": "Grace"},
    },
    {
        "label": "TEST - any open class",
        "site": "SPL",
        "date": "2026-06-14",
        "match": {"ClassName": "Cycle", "TimeString": "10:45am"},
    },
]

# ============================================================
# RUN BEHAVIOUR
# ============================================================
# This script polls in a loop for RUN_DURATION_MINUTES, then exits. The GitHub
# Actions schedule restarts it, giving near-continuous coverage with a fast
# poll interval. Keep RUN_DURATION_MINUTES slightly below your cron interval.

POLL_INTERVAL_SECONDS = 60      # how often to check. 60 is gentle and plenty fast.
RUN_DURATION_MINUTES = 14       # how long one run polls before exiting.

# ============================================================
# ENDPOINTS  (the login URL is the one piece to confirm, see README)
# ============================================================
TOKEN_URL = "https://hal.virginactive.com.sg/token"
API_URL = "https://hal.virginactive.com.sg/api/classes/bookableclassquery"

# ============================================================
# Secrets, read from the environment
# ============================================================
VA_USERNAME = os.environ.get("VA_USERNAME")
VA_PASSWORD = os.environ.get("VA_PASSWORD")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def log(msg):
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


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
            timeout=20,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log(f"Failed to send Telegram message: {e}")


def login():
    """
    Log in and return a fresh Bearer token.

    This uses the standard ASP.NET OAuth password flow, which the evidence
    strongly points to. If the `token` request you capture shows a different
    body, adjust the "data" dict below to match it.
    """
    if not VA_USERNAME or not VA_PASSWORD:
        raise SystemExit("Missing VA_USERNAME or VA_PASSWORD. Set them as secrets.")

    data = {
        "grant_type": "password",
        "username": VA_USERNAME,
        "password": VA_PASSWORD,
    }
    r = requests.post(TOKEN_URL, data=data, timeout=20)
    if r.status_code == 400:
        raise SystemExit(
            "Login was rejected (400). Double check your username and password, "
            "and confirm the token request body matches (see README)."
        )
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        raise SystemExit(f"Login succeeded but no access_token in response: {r.text[:200]}")
    log("Logged in, got a fresh token.")
    return token


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
    r = requests.post(API_URL, json=payload, headers=headers, timeout=20)
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


def run_test_telegram():
    log("Sending a Telegram test message...")
    send_telegram(
        "Test from your Virgin Active watcher. "
        "If you can read this, Telegram is wired up correctly."
    )
    log("Done. Check your phone.")


def main():
    if "--test-telegram" in sys.argv:
        run_test_telegram()
        return

    if not WATCHLIST:
        raise SystemExit("WATCHLIST is empty. Add at least one class to watch.")

    # Group entries by (site, date) so we make one API call per unique combo.
    groups = {}
    for entry in WATCHLIST:
        key = (entry["site"], entry["date"])
        groups.setdefault(key, []).append(entry)

    token = login()
    alerted = set()  # labels already alerted this run, to avoid repeat pings
    stop_at = datetime.now() + timedelta(minutes=RUN_DURATION_MINUTES)

    log(f"Watching {len(WATCHLIST)} class(es). This run lasts "
        f"{RUN_DURATION_MINUTES} min, checking every {POLL_INTERVAL_SECONDS}s.")

    while datetime.now() < stop_at:
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
                    log(f"'{label}': not found in results (check date/match fields).")
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
