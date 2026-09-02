name: Daily P&L Summary

on:
  schedule:
    # 23:00 UTC, after both the daily and hourly signal jobs have run
    # for the day, so the summary reflects everything that happened.
    - cron: '0 23 * * 1-5'
  workflow_dispatch: {}

jobs:
  summary:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install dependencies
        run: pip install pandas requests
      - name: Run daily P&L summary
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python daily_pnl_summary.py
