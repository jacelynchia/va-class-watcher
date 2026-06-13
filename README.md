# Virgin Active class watcher

Watches your Virgin Active classes and sends a Telegram message the moment a
spot opens up. Runs in the cloud on GitHub Actions, so your laptop can be off.

There are three things to set up: Telegram, your secrets, and the GitHub repo.
Budget about 30 to 45 minutes the first time. After that it just runs.

---

## Step 1: Create your Telegram bot (about 5 minutes)

1. In Telegram, search for the user **@BotFather** and start a chat.
2. Send `/newbot`. Follow the prompts to name your bot. At the end, BotFather
   gives you a **bot token** that looks like `123456789:AAignoreThisExample`.
   Save it. This is your `TELEGRAM_BOT_TOKEN`.
3. Now search for your new bot by the username you just chose, open it, and
   send it any message (for example "hi"). This is needed so the bot is
   allowed to message you.
4. Get your **chat id**: in a browser, visit this URL, replacing `<TOKEN>`
   with your bot token:

   `https://api.telegram.org/bot<TOKEN>/getUpdates`

   Look in the response for `"chat":{"id":123456789`. That number is your
   `TELEGRAM_CHAT_ID`. If you see an empty result, send your bot another
   message and refresh the URL.

---

## Step 2: Confirm the login request (about 2 minutes)

The watcher logs in by itself. It assumes the standard login format, which is
very likely correct, but please confirm it once:

1. Log in to mylocker with DevTools open, Network tab, Fetch/XHR filter.
2. Find the request named **token**.
3. Click it, open the **Payload** tab (or "View source").
4. You are looking for something like:
   `grant_type=password&username=...&password=...`

If it looks like that, you are done, no change needed. If it looks different,
note what it shows and the login function can be adjusted to match.

---

## Step 3: Put it on GitHub (about 10 minutes)

1. Create a new repository on GitHub. A **public** repo is recommended,
   because public repos get unlimited free Actions minutes. Your password is
   never in the code, it lives in encrypted Secrets, so public is safe here.
   (If you prefer a private repo, see "Cost note" below.)
2. Upload these files, keeping the folder structure:
   ```
   va_watcher.py
   requirements.txt
   .github/workflows/watch.yml
   ```
3. In the repo, go to **Settings > Secrets and variables > Actions**, and add
   four repository secrets (the names must match exactly):
   - `VA_USERNAME` your membership number, for example 110035581
   - `VA_PASSWORD` your mylocker password
   - `TELEGRAM_BOT_TOKEN` from Step 1
   - `TELEGRAM_CHAT_ID` from Step 1

---

## Step 4: Test it (this is the important bit)

You do not need to wait for a real spot to confirm it works.

**Test the Telegram wiring first.**
Run the watcher manually with the test flag, either locally
(`python va_watcher.py --test-telegram`) or, in the cloud, by temporarily
changing the run command. The simplest cloud test is the next one.

**Test the full pipeline by watching a class that is open right now.**
1. Open the app and find any class that currently has space.
2. In `va_watcher.py`, temporarily add that class to the `WATCHLIST`
   (its name, time, date, and site).
3. Go to the **Actions** tab in your repo, pick "VA class watcher", and click
   **Run workflow**. This runs it immediately instead of waiting for the timer.
4. Within a minute you should get a Telegram alert for that open class. That
   confirms login, querying, matching, and Telegram all work end to end.
5. Remove the test class from the `WATCHLIST`, leaving only your real target.

---

## Watching different or extra classes

Edit the `WATCHLIST` in `va_watcher.py`. Each entry watches one class on one
date at one club. The `match` block uses case-insensitive "contains", so you
only need enough detail to be unambiguous (class name plus time, plus
instructor if two classes share a slot). You can watch several classes at once
by adding more entries.

For a weekly recurring class, update the `date` each week. (Auto-rolling to the
next matching weekday can be added later if you want it.)

---

## Cost note

GitHub gives public repos unlimited Actions minutes, so the recommended setup
runs free. Private repos get 2000 free minutes per month, which the default
"poll for 14 minutes every 15 minutes" schedule would exceed. If you want a
private repo, raise `POLL_INTERVAL_SECONDS` and lower the cron frequency (for
example check once every 5 minutes with a short run) to stay within the limit.

---

## Honest limitations

- **Timing is not instant.** GitHub's scheduler can lag by a few minutes, and
  there can be small gaps between runs. The in-run loop polls continuously to
  minimise this, but a spot that opens and is grabbed within seconds could
  still be missed. Alerting cannot fully guarantee you catch a very fast spot.
- **Auto-booking is a separate step.** This tool alerts you, then you tap to
  book. Having the bot book automatically is possible but sits in a grey area
  with the platform terms, so it is a deliberate decision rather than a default.
- **Be a good citizen.** Polling every 60 seconds is gentle. There is no need
  to poll faster, and doing so mainly adds load to their servers.
- **Inactivity pause.** GitHub disables scheduled workflows after 60 days of no
  repo activity. A tiny commit now and then keeps it alive.
