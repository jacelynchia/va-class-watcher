name: VA class watcher

# Runs the watcher so it works 24/7 without your laptop.
# Each run polls continuously for ~5.5 hours, then the schedule starts the next
# one. Because each run is long, this gives near-continuous coverage even though
# GitHub's scheduler is not punctual. A GitHub job can run up to 6 hours, so 5.5
# leaves headroom for the handoff. You can also trigger a run by hand from the
# Actions tab (the "Run workflow" button), which is handy for testing.

on:
  schedule:
    - cron: "0 */6 * * *"
  workflow_dispatch:

jobs:
  watch:
    runs-on: ubuntu-latest
    timeout-minutes: 350
    steps:
      - name: Check out the repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run the watcher
        env:
          VA_USERNAME: ${{ secrets.VA_USERNAME }}
          VA_PASSWORD: ${{ secrets.VA_PASSWORD }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python va_watcher.py
