# Portfolio Forward Test — £1,000,000, 1% Risk Per Trade

Three validated strategies, one shared account, one journal:

| Strategy | Instrument(s) | Cadence | Backtest result |
|---|---|---|---|
| Pivot S/R (long-only, vol-scaled) | NAS100 | Daily | Sharpe 1.25 (2016-26) / 0.76 (2008-26 full history) |
| Donchian(20) (reversal) | EURGBP | Daily | Sharpe 0.58, PF 1.94 |
| ADX(14)>25 + Supertrend(10,3) | 12 pairs (EURCHF, EURCAD, EURJPY, CADCHF, NZDCAD, GBPCAD, AUDCHF, NZDJPY, GBPUSD, US30, DE30, UK100) | Hourly | Profitable full-period, IS, and OOS on all 12; robust across parameter variants |

**This places no real trades. It's a forward-test journal.**

## How risk is tracked

- Starting capital: **£1,000,000**
- Risk per trade: **1% of current equity** (compounds as equity changes)
- Every closed trade is measured in **R-multiples** (P&L ÷ risk distance at entry), then converted to £: `£P&L = R_multiple × (1% × equity_at_entry)`
- This sidesteps needing real lot-sizing/contract specs for 14 instruments across multiple currencies — R-multiples are currency-agnostic and are how prop desks track risk-based performance regardless of what's actually being traded.

**Risk reference by strategy:**
- **ADX+Supertrend**: uses its real trailing stop — genuine risk distance.
- **NAS100 / EURGBP**: these were built to exit on a target/reversal signal, not a stop, so there's no natural "risk distance" to measure R-multiples against. I added **1×ATR(14) at entry** as a risk reference, used ONLY for sizing/journaling — it does not change either strategy's actual entry or exit rules, which remain exactly as validated.

## Fresh start

All three systems begin flat, equity starts at exactly £1,000,000. This
consolidates three previously-separate repos (`nas100-pivot-signal`,
`eurgbp-donchian-signal`, `adx-supertrend-signal`) into one shared
journal — **disable the workflows in those three repos** once this one
is confirmed running, to avoid duplicate Telegram alerts. You'll lose
a few days of prior forward-test history from those repos in the
process; given how little had accumulated, that's a reasonable trade
for having one unified, honest equity curve going forward.

## Files

- `journal.py` — shared risk/R-multiple/equity logic
- `daily_signals.py` — NAS100 + EURGBP, runs once/day
- `hourly_signals.py` — 12-pair ADX+Supertrend, runs hourly
- `journal.csv` — **every closed trade**, across all 3 strategies: entry, exit, risk reference, R-multiple, £ risked, £ P&L, running equity
- `equity.json` — current account equity
- `nas100_log.csv`, `eurgbp_log.csv`, `hourly_price_log.csv` — price history per system (needed for indicator calculation)
- `daily_state.json`, `hourly_state.json` — live open-position state per system
- `.github/workflows/daily.yml`, `.github/workflows/hourly.yml` — the two cron jobs

## Setup

1. Push this folder as a new repo.
2. Add secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
3. Settings → Actions → General → Workflow permissions → Read and write → Save.
4. Actions tab → run **both** workflows manually once to confirm green.
5. From then on: daily runs after the daily close, hourly runs every hour on weekdays.

## Data source & caveats

Yahoo Finance, no key needed. NAS100 uses `^NDX` (real index, close to
broker CFD levels — a fix from an earlier version of this project that
used QQQ as a proxy and was wildly off-scale). EURGBP and the 12 hourly
pairs use standard `=X` FX tickers. US30/DE30/UK100 use `^DJI`/`^GDAXI`/
`^FTSE` — real index values, not identical to your broker's CFD quote,
but much closer than a proxy ETF.

## Reading the journal

Open `journal.csv` any time to see every closed trade across all three
strategies, in order, with the running equity after each one. That
equity progression — not any individual trade — is the actual measure
of whether this is working.
