# Strat 1 — Forward Test

**Current members (4): Pivot S/R, Overnight Extension, Divergence-Fade, COT Positioning Extreme**
Backtested: CAGR 9.86%, MaxDD exactly -6.00%, Sharpe 1.629.

**All 4 strategies are now genuinely complete, integrated, and audited** — this
package is ready to deploy as-is (aside from `execution.py`/`oanda_client.py`
if you want live execution, and the two secrets below).

## Admission rule — read before ever adding a strategy here

A new strategy is added to Strat 1 **only if** re-running the full exhaustive
combinatorial search — across the current members plus the new candidate —
finds a combination that **genuinely improves CAGR at the exact same 6% MaxDD
ceiling**, verified by the same binary-search-to-exact-6%-MaxDD method used
to build this configuration. Not "looks good alone." Not "positive Sharpe."
A real, re-verified improvement to the actual combined number.

If it doesn't pass: it goes in **Capital** instead (`journal_capital_config.py`),
which has no admission bar because it has no drawdown ceiling to protect.

Every strategy not currently listed here (Donchian, Connors RSI, Monday
Effect, RSI(2), TGA-Follow, VIX Shock-Fade) has already been tested against
this bar and did not improve on the current 4.

## What's in this folder — built, integrated, and audited

- `journal.py` — risk/equity logic, Strat 1 sizing only
- `pivot_sr_signals.py`, `nas100_state.json`, `nas100_signal_log.csv` (seeded, 520 days real NAS100 history), `.github/workflows/pivot_sr_signals.yml`
  — rewritten from the original standalone version to use the shared `journal.py` risk/equity system (the original never did — it sized and logged independently, invisible to the 10% total-risk cap). Signal logic itself (pivot/resistance/vol-scale calculation) unchanged. Tested end-to-end: entry and exit both confirmed working through the shared journal.
- `cot_signals.py`, `cot_full_state.json`, `.github/workflows/cot_signals.yml`
- `divergence_signals.py`, `divergence_full_state.json`, `.github/workflows/divergence_signals.yml`
- `overnight_extension_signals.py`, `overnight_extension_state.json`, `.github/workflows/overnight_extension_signals.yml`
  — audited 2026-09-22 for the specific lookahead-bug pattern that broke ADX+Supertrend/QM+CISD+SBR; confirmed clean
- `daily_pnl_summary.py`, `.github/workflows/daily_pnl_summary.yml`
- `equity.json`, `journal.csv` — fresh, £1,000,000 starting equity

## What's NOT in this folder

- `execution.py` and `oanda_client.py`, if you're using live execution
  (these were never rebuilt in this conversation — copy unchanged from the old repo)

## Setup

1. Push this folder as a new repo (or clear the old one down to just this).
2. Copy over the 2 missing scripts above from the old repo, unchanged.
3. Add secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
4. Settings → Actions → Workflow permissions → Read and write.
5. Run every workflow manually once, confirm all green before trusting the schedule.
