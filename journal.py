"""
Shared journal + equity tracking for the portfolio forward test.

Design:
- Starting capital: £1,000,000
- Risk per trade: a per-strategy % of CURRENT equity (compounds as equity
  changes), subject to two safety caps added after reviewing what unbounded
  sizing actually does over many trades and many simultaneous positions:

  1. TOTAL OPEN RISK CAP (10%): before sizing a new trade, this checks how
     much risk is already committed across ALL strategies combined and
     caps the combined risk of everything open at once. On a day where
     many signals fire together, this stops that from meaning double-digit
     % of the account is on the line simultaneously — new trades get sized
     down to fit the remaining budget, or skipped entirely if the budget
     is already full.

  2. COMPOUNDING CAP: position size is capped at a fixed multiple of
     STARTING capital, not current (compounded) equity. Backtesting this
     without a cap produced impossible position sizes after enough winning
     trades — no real market absorbs that. This keeps sizing sane
     regardless of how much the account has grown.

- PER-STRATEGY RISK: several strategies run at 0.5% instead of the
  standard 1%, after audits showed thinner margins or confirmed tail risk
  at full sizing. See STRATEGY_RISK_PCT below — everything not listed
  there uses the default.

- Every trade's outcome is tracked as an R-multiple (P&L in price terms
  divided by the risk distance in price terms), because many instruments
  in different currencies (JPY crosses, USD indices, EUR/GBP pairs) mean
  actual lot-sizing needs real broker contract specs this tool doesn't
  have. R-multiples sidestep that honestly: "you risked X% of equity;
  this trade returned +2.3x that risk" converts cleanly to £ regardless
  of what currency the instrument itself is priced in.
- £ P&L for a closed trade = R_multiple * risked_gbp (risked_gbp reflects
  that strategy's own risk %, whichever cap ended up binding, if any).
- Equity updates trade-by-trade as trades close (compounding).

For strategies that don't have a hard stop-loss on the daily systems
(they exit on a target/reversal signal, not a stop), the "risk distance"
used for R-multiple purposes is a reference price (typically 1x ATR(14)
at entry) — added purely for sizing/journaling, does NOT change their
actual entry/exit rules.
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
QM_STATE_PATH = BASE / "qm_state.json"
OVERNIGHT_STATE_PATH = BASE / "overnight_extension_state.json"
DIVERGENCE_STATE_PATH = BASE / "divergence_full_state.json"

STARTING_EQUITY = 1_000_000.0
RISK_PCT = 0.01                        # default: 1% of current equity, per trade, before caps
STRATEGY_RISK_PCT = {
    "Pivot S/R": 0.005,                 # reduced after audit - see module docstring
    "Connors RSI": 0.005,                # reduced given thin absolute margin (backtested PF 1.02-1.14),
                                          # despite unusually strong cost-stress robustness - see daily_signals.py
    "RSI(2) Mean Reversion": 0.005,       # reduced given a REAL confirmed tail risk - bootstrap worst-case
                                          # drawdown -65.3% at standard 1% sizing (directional, non-hedged
                                          # positions) - see daily_signals.py
    "Divergence-Fade": 0.005,            # reduced given a smaller validated sample (~100 trades/instrument)
                                          # than the longer-running strategies - see divergence_signals.py
}
MAX_TOTAL_OPEN_RISK_PCT = 0.10         # 10% combined risk cap across all simultaneously open positions
MAX_RISK_MULTIPLE_OF_STARTING = 5      # position size never exceeds 5x what that strategy's risk % of STARTING capital would be

JOURNAL_HEADERS = [
    "close_timestamp", "instrument", "strategy", "direction",
    "entry_price", "risk_reference_price", "exit_price",
    "risk_distance", "r_multiple", "risked_gbp", "pnl_gbp",
    "equity_before", "equity_after", "risk_fraction_applied",
]


def get_risk_pct(strategy):
    """Per-strategy risk %, falling back to the RISK_PCT default for
    anything not explicitly listed in STRATEGY_RISK_PCT."""
    return STRATEGY_RISK_PCT.get(strategy, RISK_PCT)


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


def compute_committed_risk_pct():
    """Sums the ACTUAL risk % already committed by every currently-open
    position across all strategies, correctly weighting each strategy's
    own risk %. Used for the total-open-risk cap. Reads all state files
    directly."""
    committed = 0.0
    if DAILY_STATE_PATH.exists():
        daily_state = json.loads(DAILY_STATE_PATH.read_text())
        if daily_state.get("nas100", {}).get("state", 0) != 0:
            committed += get_risk_pct("Pivot S/R")
        for inst_state in daily_state.get("donchian", {}).values():
            if inst_state.get("state", 0) != 0:
                committed += get_risk_pct("Donchian(20)")
        for inst_state in daily_state.get("connors", {}).values():
            if inst_state.get("state", 0) != 0:
                committed += get_risk_pct("Connors RSI")
        for inst_state in daily_state.get("monday_effect", {}).values():
            if inst_state.get("state", 0) != 0:
                committed += get_risk_pct("Monday Effect")
        for inst_state in daily_state.get("rsi2", {}).values():
            if inst_state.get("state", 0) != 0:
                committed += get_risk_pct("RSI(2) Mean Reversion")
    if HOURLY_STATE_PATH.exists():
        hourly_state = json.loads(HOURLY_STATE_PATH.read_text())
        for inst_state in hourly_state.values():
            if inst_state.get("state", 0) != 0:
                committed += get_risk_pct("ADX+Supertrend")
    if QM_STATE_PATH.exists():
        qm_state = json.loads(QM_STATE_PATH.read_text())
        for inst_state in qm_state.values():
            if inst_state.get("order") is not None:
                committed += get_risk_pct("QM+CISD+SBR")
    if OVERNIGHT_STATE_PATH.exists():
        overnight_state = json.loads(OVERNIGHT_STATE_PATH.read_text())
        for inst_state in overnight_state.values():
            if inst_state.get("state", 0) != 0:
                committed += get_risk_pct("Overnight Extension")
    if DIVERGENCE_STATE_PATH.exists():
        divergence_full_state = json.loads(DIVERGENCE_STATE_PATH.read_text())
        divergence_state = divergence_full_state.get("divergence", {})
        for inst_state in divergence_state.values():
            if inst_state.get("state", 0) != 0:
                committed += get_risk_pct("Divergence-Fade")
    return committed


def available_risk_fraction(strategy, extra_committed_pct=0.0):
    """
    Returns how much of a full risk slice (0.5% for reduced-risk
    strategies, 1% for everything else) a NEW trade in the given strategy
    is allowed to use, given how much total risk is already open across
    the whole portfolio.

    extra_committed_pct: risk percentage POINTS already committed EARLIER
    IN THE SAME RUN that haven't been saved to disk yet — pass a running
    total (in percentage points, not a position count, since different
    strategies now commit different amounts per position) so a second or
    third new entry in the same run doesn't undercount what's already
    been committed.

    1.0  = full slice available (plenty of room under the 10% cap)
    0-1  = partial — some room left, new trade gets sized down to fit
    0.0  = no room — the 10% cap is already full, skip this trade entirely
    """
    committed = compute_committed_risk_pct() + extra_committed_pct
    remaining_budget_pct = MAX_TOTAL_OPEN_RISK_PCT - committed
    desired_slice = get_risk_pct(strategy)
    if remaining_budget_pct <= 0:
        return 0.0
    return min(1.0, remaining_budget_pct / desired_slice)


def current_risk_gbp(strategy, risk_fraction=1.0):
    """£ amount a new trade in the given strategy should risk, applying
    both safety caps: the compounding cap (vs starting capital, using that
    strategy's own risk %) and whatever fraction of a full slice the
    total-open-risk budget allows (see available_risk_fraction — pass
    that in explicitly at the call site so the same number used for
    sizing is also loggable in the journal)."""
    equity = load_equity()
    risk_pct = get_risk_pct(strategy)
    uncapped = risk_pct * equity
    compounding_cap = MAX_RISK_MULTIPLE_OF_STARTING * risk_pct * STARTING_EQUITY
    base_risk = min(uncapped, compounding_cap)
    return base_risk * risk_fraction


def record_trade_close(instrument, strategy, direction, entry_price, risk_reference_price, exit_price, risk_fraction_at_entry=1.0):
    """
    direction: 'long' or 'short'
    risk_reference_price: the stop / risk-distance reference at entry
      (actual trailing stop where one exists; a reference price like
      1xATR(14) for strategies that exit on target/reversal, not a stop)
    risk_fraction_at_entry: whatever available_risk_fraction() returned
      when this trade was opened — needed so P&L matches what was actually
      risked, not a fresh full slice recomputed at close time.
    Returns the £ P&L for this trade and the new equity.
    """
    ensure_journal_exists()
    equity_before = load_equity()
    risk_pct = get_risk_pct(strategy)

    risk_distance = abs(entry_price - risk_reference_price)
    if risk_distance == 0:
        r_multiple = 0.0
    else:
        raw_move = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
        r_multiple = raw_move / risk_distance

    uncapped = risk_pct * equity_before
    compounding_cap = MAX_RISK_MULTIPLE_OF_STARTING * risk_pct * STARTING_EQUITY
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


def build_daily_pnl_summary():
    """Builds a plain-text daily P&L summary: current equity, total P&L
    since inception, and today's closed trades specifically (from
    journal.csv, filtered to today's UTC date). Designed to be sent as
    its own Telegram message once a day, separate from each strategy's
    own per-signal messages."""
    equity = load_equity()
    total_pnl = equity - STARTING_EQUITY
    total_pnl_pct = (equity / STARTING_EQUITY - 1) * 100

    lines = [
        "DAILY P&L SUMMARY",
        f"Current equity: £{equity:,.2f}",
        f"Total P&L since inception: £{total_pnl:,.2f} ({total_pnl_pct:+.2f}%)",
        "",
    ]

    if not JOURNAL_PATH.exists():
        lines.append("No trades recorded yet.")
        return "\n".join(lines)

    today = datetime.now(timezone.utc).date()
    todays_trades = []
    with open(JOURNAL_PATH, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["close_timestamp"])
            except (KeyError, ValueError):
                continue
            if ts.date() == today:
                todays_trades.append(row)

    if not todays_trades:
        lines.append("No trades closed today.")
    else:
        todays_pnl = sum(float(t["pnl_gbp"]) for t in todays_trades)
        lines.append(f"Today's closed trades: {len(todays_trades)}, total P&L: £{todays_pnl:,.2f}")
        lines.append("")
        for t in todays_trades:
            sign = "+" if float(t["pnl_gbp"]) >= 0 else ""
            lines.append(f"  {t['instrument']} ({t['strategy']}, {t['direction']}): {sign}£{float(t['pnl_gbp']):,.2f} ({float(t['r_multiple']):+.2f}R)")

    return "\n".join(lines)
