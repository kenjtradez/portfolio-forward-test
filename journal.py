"""
Shared journal + equity tracking for the portfolio forward test.

Design:
- Starting capital: £1,000,000
- Risk per trade: 1% of CURRENT equity (compounds as equity changes),
  subject to two safety caps added after reviewing what unbounded sizing
  actually does over many trades and many simultaneous positions:

  1. TOTAL OPEN RISK CAP (10%): before sizing a new trade, this checks how
     many positions are already open across ALL THREE strategies combined
     (NAS100, the 7 Donchian instruments, the 34 ADX+Supertrend instruments)
     and caps the combined risk of everything open at once. On a day where
     many signals fire together (seen up to 15 in backtesting), this stops
     that from meaning 15% of the account is on the line simultaneously —
     new trades get sized down to fit the remaining budget, or skipped
     entirely if the budget is already full.

  2. COMPOUNDING CAP: position size is capped at a fixed multiple of
     STARTING capital, not current (compounded) equity. Backtesting this
     without a cap produced impossible position sizes after enough winning
     trades — no real market absorbs that. This keeps sizing sane
     regardless of how much the account has grown.

- Every trade's outcome is tracked as an R-multiple (P&L in price terms
  divided by the risk distance in price terms), because 14+ instruments
  in different currencies (JPY crosses, USD indices, EUR/GBP pairs) mean
  actual lot-sizing needs real broker contract specs this tool doesn't
  have. R-multiples sidestep that honestly: "you risked X% of equity;
  this trade returned +2.3x that risk" converts cleanly to £ regardless
  of what currency the instrument itself is priced in.
- £ P&L for a closed trade = R_multiple * risked_gbp (risked_gbp reflects
  whichever cap above ended up binding, if any).
- Equity updates trade-by-trade as trades close (compounding).

For NAS100 and EURGBP, which don't have a hard stop-loss (they exit on
a target/reversal signal, not a stop), the "risk distance" used for R-
multiple purposes is 1x ATR(14) at entry — added purely for sizing/
journaling, does NOT change their actual entry/exit rules.
"""
import json
import csv
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).parent
EQUITY_PATH = BASE / "equity.json"
JOURNAL_PATH = BASE / "journal.csv"
DAILY_STATE_PATH = BASE / "daily_state.json"
HOURLY_STATE_PATH = BASE / "hourly_state.json"

STARTING_EQUITY = 1_000_000.0
RISK_PCT = 0.01                        # 1% of current equity, per trade, before caps
MAX_TOTAL_OPEN_RISK_PCT = 0.10         # 10% combined risk cap across all simultaneously open positions
MAX_RISK_MULTIPLE_OF_STARTING = 5      # position size never exceeds 5x what 1% of STARTING capital would be

JOURNAL_HEADERS = [
    "close_timestamp", "instrument", "strategy", "direction",
    "entry_price", "risk_reference_price", "exit_price",
    "risk_distance", "r_multiple", "risked_gbp", "pnl_gbp",
    "equity_before", "equity_after", "risk_fraction_applied",
]


def load_equity():
    if EQUITY_PATH.exists():
        return json.loads(EQUITY_PATH.read_text())["equity"]
    return STARTING_EQUITY


def save_equity(equity):
    EQUITY_PATH.write_text(json.dumps({"equity": equity, "starting_equity": STARTING_EQUITY}, indent=2))


def ensure_journal_exists():
    if not JOURNAL_PATH.exists():
        with open(JOURNAL_PATH, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(JOURNAL_HEADERS)


def count_open_positions():
    """Counts currently-open positions across all three strategies by
    reading both state files directly. Used for the total-open-risk cap."""
    count = 0
    if DAILY_STATE_PATH.exists():
        daily_state = json.loads(DAILY_STATE_PATH.read_text())
        if daily_state.get("nas100", {}).get("state", 0) != 0:
            count += 1
        for inst_state in daily_state.get("donchian", {}).values():
            if inst_state.get("state", 0) != 0:
                count += 1
    if HOURLY_STATE_PATH.exists():
        hourly_state = json.loads(HOURLY_STATE_PATH.read_text())
        for inst_state in hourly_state.values():
            if inst_state.get("state", 0) != 0:
                count += 1
    return count


def available_risk_fraction(extra_open=0):
    """
    Returns how much of a full 1% risk slice a NEW trade is allowed to use,
    given how much total risk is already open across the whole portfolio.

    extra_open: positions opened EARLIER IN THE SAME RUN that haven't been
    saved to disk yet (state files are only written at the end of each
    script's main()) — pass a running counter so a second or third new
    entry in the same run doesn't undercount what's already been committed.

    1.0  = full 1% available (plenty of room under the 10% cap)
    0-1  = partial — some room left, new trade gets sized down to fit
    0.0  = no room — the 10% cap is already full, skip this trade entirely
    """
    open_positions = count_open_positions() + extra_open
    already_committed_pct = open_positions * RISK_PCT
    remaining_budget_pct = MAX_TOTAL_OPEN_RISK_PCT - already_committed_pct
    if remaining_budget_pct <= 0:
        return 0.0
    return min(1.0, remaining_budget_pct / RISK_PCT)


def current_risk_gbp(risk_fraction=1.0):
    """£ amount a new trade should risk, applying both safety caps:
    the compounding cap (vs starting capital) and whatever fraction of
    a full 1% slice the total-open-risk budget allows (see
    available_risk_fraction — pass this in explicitly at the call site
    so the same number used for sizing is also loggable in the journal)."""
    equity = load_equity()
    uncapped = RISK_PCT * equity
    compounding_cap = MAX_RISK_MULTIPLE_OF_STARTING * RISK_PCT * STARTING_EQUITY
    base_risk = min(uncapped, compounding_cap)
    return base_risk * risk_fraction


def record_trade_close(instrument, strategy, direction, entry_price, risk_reference_price, exit_price, risk_fraction_at_entry=1.0):
    """
    direction: 'long' or 'short'
    risk_reference_price: the stop / risk-distance reference at entry
      (actual trailing stop for ADX+Supertrend; 1xATR(14) reference for
      NAS100/EURGBP, which don't have a hard stop)
    risk_fraction_at_entry: whatever available_risk_fraction() returned
      when this trade was opened — needed so P&L matches what was actually
      risked, not a fresh full 1% recomputed at close time.
    Returns the £ P&L for this trade and the new equity.
    """
    ensure_journal_exists()
    equity_before = load_equity()

    risk_distance = abs(entry_price - risk_reference_price)
    if risk_distance == 0:
        r_multiple = 0.0
    else:
        raw_move = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
        r_multiple = raw_move / risk_distance

    uncapped = RISK_PCT * equity_before
    compounding_cap = MAX_RISK_MULTIPLE_OF_STARTING * RISK_PCT * STARTING_EQUITY
    risked_gbp = min(uncapped, compounding_cap) * risk_fraction_at_entry
    pnl_gbp = r_multiple * risked_gbp
    equity_after = equity_before + pnl_gbp

    with open(JOURNAL_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now(timezone.utc).isoformat(), instrument, strategy, direction,
            round(entry_price, 6), round(risk_reference_price, 6), round(exit_price, 6),
            round(risk_distance, 6), round(r_multiple, 3), round(risked_gbp, 2), round(pnl_gbp, 2),
            round(equity_before, 2), round(equity_after, 2), round(risk_fraction_at_entry, 3),
        ])

    save_equity(equity_after)
    return pnl_gbp, equity_after
