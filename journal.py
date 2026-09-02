"""
Shared journal + equity tracking for the portfolio forward test.

Design:
- Starting capital: £1,000,000
- Risk per trade: 1% of CURRENT equity (compounds as equity changes)
- Every trade's outcome is tracked as an R-multiple (P&L in price terms
  divided by the risk distance in price terms), because 14 instruments
  in different currencies (JPY crosses, USD indices, EUR/GBP pairs) mean
  actual lot-sizing needs real broker contract specs this tool doesn't
  have. R-multiples sidestep that honestly: "you risked 1% of equity;
  this trade returned +2.3x that risk" converts cleanly to £ regardless
  of what currency the instrument itself is priced in.
- £ P&L for a closed trade = R_multiple * (0.01 * equity_at_entry)
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

STARTING_EQUITY = 1_000_000.0
RISK_PCT = 0.01

JOURNAL_HEADERS = [
    "close_timestamp", "instrument", "strategy", "direction",
    "entry_price", "risk_reference_price", "exit_price",
    "risk_distance", "r_multiple", "risked_gbp", "pnl_gbp",
    "equity_before", "equity_after",
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


def record_trade_close(instrument, strategy, direction, entry_price, risk_reference_price, exit_price):
    """
    direction: 'long' or 'short'
    risk_reference_price: the stop / risk-distance reference at entry
      (actual trailing stop for ADX+Supertrend; 1xATR(14) reference for
      NAS100/EURGBP, which don't have a hard stop)
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

    risked_gbp = RISK_PCT * equity_before
    pnl_gbp = r_multiple * risked_gbp
    equity_after = equity_before + pnl_gbp

    with open(JOURNAL_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now(timezone.utc).isoformat(), instrument, strategy, direction,
            round(entry_price, 6), round(risk_reference_price, 6), round(exit_price, 6),
            round(risk_distance, 6), round(r_multiple, 3), round(risked_gbp, 2), round(pnl_gbp, 2),
            round(equity_before, 2), round(equity_after, 2),
        ])

    save_equity(equity_after)
    return pnl_gbp, equity_after


def current_risk_gbp():
    """£ amount that 1% of current equity represents, for Telegram messages."""
    return RISK_PCT * load_equity()
