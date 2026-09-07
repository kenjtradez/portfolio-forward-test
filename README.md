# Portfolio Forward Test — £1,000,000, Per-Strategy Risk

**Live now: NAS100 Pivot S/R, the 11-instrument Donchian(20) basket, and Connors RSI Composite (10 instruments).**
ADX+Supertrend and QM+CISD+SBR were removed after a full mechanical audit
found real lookahead bugs in both — see "Removed strategies" below before
ever re-enabling either.

| Strategy | Instrument(s) | Cadence | Risk/trade | True backtest result (post-audit) |
|---|---|---|---|---|
| Pivot S/R (long-only, vol-scaled) | NAS100 | Daily | 0.5% | Sharpe 0.91 (full 2008-26 history, causal vol-scaling) |
| Donchian(20) (7 variants) | 11 instruments | Daily | 1% | Sharpe 0.36-0.58 per instrument, unaffected by the audit |
| Connors RSI Composite | 10 instruments | Daily | 0.5% | PF 1.02-1.14 per instrument, 100th-percentile randomization test, correlation 0.5-0.6 with the other two live strategies |

**This places no real trades. It's a forward-test journal.**

## Removed strategies — DO NOT re-enable without a full rebuild + fresh audit

**ADX+Supertrend** (34 instruments, hourly) and **QM+CISD+SBR** (34
instruments, 1H→15m) both looked extensively validated — multi-instrument
sweeps, in-sample/out-of-sample splits, parameter sensitivity, even a
2000-simulation randomization test showing results ~10σ and ~8σ beyond
chance. All of that was real, and all of it was validating a bug, not a
genuine edge:

- **ADX+Supertrend**: the trailing stop was ratcheted using each bar's own
  Supertrend value (which needs that bar's own full high/low range to
  compute), then tested against that *same* bar's high/low — letting the
  stop "see" the bar's range before deciding whether it had been breached.
  Present in both the backtest and the live code. Fixed and re-tested:
  true edge is negative, ~10 standard deviations *below* random chance,
  across all 34 instruments and multiple alternative stop designs.
- **QM+CISD+SBR**: the backtest's multi-timeframe alignment anchored the
  15-minute lookup on the 1-hour bar's *label* instead of its actual close
  time (H1 bars are left-labeled — the "04:00" bar covers 04:00-05:00 and
  isn't known until 05:00). This let the origin-candle search and fill
  check use up to 45 minutes of price action from *before* the signal
  existed. The live code was naturally immune (real API calls can't return
  future bars), but the backtest that justified deploying it was not.
  Fixed and re-tested: true edge is negative, ~2.6σ below random chance.

Both are the exact same lesson: statistical rigor (randomization tests,
IS/OOS splits, parameter sweeps) proves a backtested pattern isn't random
noise — it cannot prove the backtest was simulated correctly. A structural
bug that consistently biases results the same way will pass every one of
those tests, because none of them ask "could this specific value have
existed at this specific moment." That mechanical trace has to happen
*first*, for every strategy, before any statistical layer is trusted.

Their code and disabled workflow files are kept in this repo as an audit
trail (see `.github/workflows/hourly.yml` and `.github/workflows/qm_signals.yml`,
both have `schedule:` removed, `workflow_dispatch` only) — not as
something to casually turn back on.

## Fresh start (3rd reset)

Equity is back to exactly £1,000,000, journal is empty, every position
across all four strategies' state files starts flat. Two strategies are
now permanently disabled (see above); NAS100 Pivot and Donchian continue
under a revised, per-strategy risk model (see below).

## How risk is tracked

- Starting capital: **£1,000,000**
- Risk per trade: **per-strategy %** of current equity (compounds), capped
  at 5x what that % of *starting* capital would be, and capped in total
  across all simultaneously open positions at **10%**
- **NAS100 Pivot runs at 0.5%**, half the standard rate — added after an
  audit found it was the most fragile of the original four on cost
  sensitivity, walk-forward consistency, and bootstrap drawdown range,
  even though its edge itself is genuine
- Donchian runs at the standard **1%**
- Every closed trade is measured in **R-multiples** (P&L ÷ risk distance at entry), then converted to £: `£P&L = R_multiple × (that strategy's risk% × equity_at_entry)`
- This sidesteps needing real lot-sizing/contract specs for instruments across multiple currencies — R-multiples are currency-agnostic and are how prop desks track risk-based performance regardless of what's actually being traded.

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

## OANDA Live/Practice Execution (optional — off by default)

By default, everything above runs exactly as before: signal-only, logs to
the journal, alerts on Telegram, **places no real orders**. Adding real
execution is opt-in and requires deliberate setup — nothing changes until
you add the OANDA secrets below.

### What gets added when execution is enabled
- `oanda_client.py` — thin wrapper for OANDA's v20 REST API (order placement, stop management, account balance, pricing)
- `instrument_map.py` — maps this portfolio's instrument names to OANDA's exact tradeable symbols
- `execution.py` — converts your 1%-of-equity risk (in £) into the correct position size per instrument, handling currency conversion via OANDA's own live pricing, and places/closes/trails orders

### Setup, in order
1. **Verify instrument mappings first.** Some of `instrument_map.py`'s guesses (particularly indices — DE30, UK100, etc.) may not match OANDA's exact current symbol. Run this locally or as a one-off script, using your OANDA practice API token:
   ```
   python instrument_map.py
   ```
   Fix anything it flags before proceeding.

2. **Add secrets** (Settings → Secrets and variables → Actions):
   - `OANDA_API_TOKEN` — your personal access token, generated in your OANDA account
   - `OANDA_ACCOUNT_ID` — your OANDA account ID
   - `OANDA_ENVIRONMENT` — set to `practice` (do this first — do NOT set to `live` until you've watched practice run cleanly for a real stretch of time)

3. **Run both workflows manually** and confirm:
   - The Telegram message shows `[LIVE TRADING]` (this is correct — OANDA's practice environment is functionally live order routing, just with fake money) with your **practice account balance**, not the £1,000,000 journal figure
   - Trades actually appear in your OANDA practice account when a signal fires

4. **Only once you're satisfied**, change `OANDA_ENVIRONMENT` to `live`. This is a real, irreversible switch — real orders, real money, from the next run onward.

### Safety notes
- NAS100 and EURGBP Donchian have no hard stop in their original backtested logic (they exit on target/reversal). For live execution, a real protective stop is attached anyway (using the same ATR risk-reference already computed) — an unprotected live position is a bad idea regardless of what the backtest assumed. This is a deliberate addition, not a change to the tested edge.
- Stop-triggered exits are handled natively by OANDA once the stop is attached — the script doesn't need to (and doesn't try to) close those manually. Only reversal/target-driven exits get an explicit close call.
- £1,000 sizing was checked for realistic spread drag before this was built (see conversation) — GBPNZD-type wide-spread crosses carry the most drag (~5% of risked capital per trade); everything else is in the 1-3% range, consistent with backtest cost assumptions.
- Re-verify your position sizing against ANY prop firm's specific drawdown rules before using this on prop capital — the 1% figure was never checked against a specific firm's limits.

## Reading the journal

Open `journal.csv` any time to see every closed trade across all three
strategies, in order, with the running equity after each one. That
equity progression — not any individual trade — is the actual measure
of whether this is working.
