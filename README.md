# Portfolio Forward Test — £1,000,000, 1% Risk Per Trade

Three validated strategies plus one experimental one, one shared account, one journal — **now covering all 34 instruments** the ADX+Supertrend system has been validated on (expanded from an initial 12-pair pilot; all 34 came back profitable, in-sample and out-of-sample, no exceptions).

| Strategy | Instrument(s) | Cadence | Backtest result |
|---|---|---|---|
| Pivot S/R (long-only, vol-scaled) | NAS100 | Daily | Sharpe 1.25 (2016-26) / 0.76 (2008-26 full history) |
| Donchian(20) (reversal) | EURGBP | Daily | Sharpe 0.58, PF 1.94 |
| ADX(14)>25 + Supertrend(10,3) | **All 34 instruments** (full FX/index/gold universe tested) | Hourly | Profitable on 34/34, IS and OOS, across every instrument tested |
| Level Continuation *(experimental — see caveat below)* | Gold (XAUUSD), NAS100 | Daily | **Not backtested.** Built from marginal touch/reversal stats only — see caveat |

**This places no real trades. It's a forward-test journal.**

**Caveat on Level Continuation:** unlike the other three rows, this one has no backtest behind it yet. It's built from `reversal_backtest.json`'s marginal reversal rates (NAS100 continues through a touched level >50% of the time at every tier tested; Gold continues only 38–46% of the time), which only tells you the odds from a day's open — not the conditional odds this strategy actually depends on (given price already broke the median level, what's the chance it goes on to reach the 75th-percentile target before the stop). Treat its forward-test numbers as a live experiment, not a validated system, until that conditional backtest is done. Full design rationale in the `gold_nas_target_signals.py` docstring.

## Fresh start (2nd reset)

This repo was reset once already to consolidate three prior single-strategy repos.
It has now been reset **again** to expand the hourly system from its original
12-pair pilot to the full 34-instrument universe. Equity is back to exactly
£1,000,000, journal is empty, all 34+2 positions start flat. This is a clean
slate specifically because the instrument set materially changed — comparing
against the old 12-pair equity curve wouldn't be meaningful once the book is
this much bigger.

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
- `gold_nas_target_signals.py` — Gold + NAS100 Level Continuation (experimental), runs once/day
- `journal.csv` — **every closed trade**, across all strategies: entry, exit, risk reference, R-multiple, £ risked, £ P&L, running equity
- `equity.json` — current account equity
- `nas100_log.csv`, `eurgbp_log.csv`, `hourly_price_log.csv`, `gold_nas_target_log.csv` — price history per system (needed for indicator calculation)
- `daily_state.json`, `hourly_state.json`, `gold_nas_target_state.json` — live open-position state per system
- `.github/workflows/daily.yml`, `.github/workflows/gold_nas_target.yml` — the daily cron jobs (`hourly.yml` referenced below is not currently present in this repo — see caveat)
- `.github/workflows/daily_pnl_summary.yml` — end-of-day digest across every strategy

**Note:** `hourly_signals.py` (the 34-instrument ADX+Supertrend system) exists in this repo but there's currently no `.github/workflows/hourly.yml` scheduling it — so, as of this write-up, it isn't actually running on a schedule despite the README elsewhere describing it as hourly. Worth adding that workflow (mirroring `daily.yml`'s structure with an hourly cron) if that strategy is meant to be live.

## Setup

1. Push this folder as a new repo.
2. Add secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
3. Settings → Actions → General → Workflow permissions → Read and write → Save.
4. Actions tab → run each workflow manually once (`workflow_dispatch`) to confirm green.
5. From then on: `daily.yml` and `gold_nas_target.yml` run after the daily close, `daily_pnl_summary.yml` runs at day's end.

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
