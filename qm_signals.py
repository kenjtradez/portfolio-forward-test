name: QM+CISD+SBR Signals (34 instruments, 1H->15m)

on:
  schedule:
    # every 30 minutes, weekdays. Was every 15 min - reduced after hitting
    # Yahoo Finance rate limits (34 instruments x 2 timeframes = 68 requests
    # per run was too aggressive for their unofficial free API). A limit-order
    # strategy with hours-long typical holds doesn't need faster polling than
    # this to trade correctly - the fill/stop/target check still scans every
    # 15m bar since the order was placed, not just the latest one.
    - cron: '*/30 * * * 1-5'
  workflow_dispatch: {}

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install dependencies
        run: pip install pandas requests numpy
      - name: Run QM+CISD+SBR signals
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          OANDA_API_TOKEN: ${{ secrets.OANDA_API_TOKEN }}
          OANDA_ACCOUNT_ID: ${{ secrets.OANDA_ACCOUNT_ID }}
          OANDA_ENVIRONMENT: ${{ secrets.OANDA_ENVIRONMENT }}
        run: python qm_signals.py
      - name: Commit updated state
        run: |
          git config user.name "github-actions"
          git config user.email "actions@github.com"
          git add qm_state.json equity.json journal.csv
          git diff --staged --quiet || git commit -m "QM signal update $(date -u +%Y-%m-%dT%H:%M)"
          git pull --rebase --autostash
          git push
