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
COT_STATE_PATH = BASE / "cot_full_state.json"
VIXFADE_STATE_PATH = BASE / "vixfade_full_state.json"
TGAFOLLOW_STATE_PATH = BASE / "tgafollow_full_state.json"

STARTING_EQUITY = 1_000_000.0
RISK_PCT = 0.01                        # default: 1% of current equity, per trade, before caps
STRATEGY_RISK_PCT = {
    # FUNDED CONFIGURATION: "STRAT 1" (2026-09-20) - found via exhaustive
    # search over all 1,013 possible non-empty combinations of the 10
    # validated strategies, each rescaled to exactly 6% MaxDD. This
    # combination genuinely maximizes both CAGR and Sharpe simultaneously
    # vs the earlier 3-strategy version: CAGR 9.86% (was 9.24%), MaxDD
    # exactly -6.00%, Sharpe 1.629 (was 1.586). Divergence-Fade added on
    # top of the prior best (Pivot S/R + Overnight Extension + COT) -
    # confirmed via the full correlation matrix to be one of 6 genuinely
    # near-zero-correlated strategies (not just picked because it scored
    # well), so this is real diversification, not double-counting risk.
    #
    # Other "Strat N" configurations exist as separate reference files
    # for different risk/complexity trade-offs found in the same search
    # - see journal_strat2_config.py etc. Only ONE strat should be active
    # in this file at a time.
    #
    # The remaining strategies not listed here are NOT deleted - they
    # remain validated and could be reconsidered - but are set to 0
    # deliberately, not simply removed, so that if any of their GitHub
    # Actions workflows are accidentally left enabled, they size at £0
    # (a no-op) rather than silently falling back to the 1% RISK_PCT
    # default - the exact mistake that caused the VIX Shock-Fade
    # drawdown problem earlier.
    "Pivot S/R": 0.0034427,
    "Overnight Extension": 0.0068068,
    "Divergence-Fade": 0.0032725,
    "COT Positioning Extreme": 0.0024217,
    "Donchian(20)": 0,
    "Connors RSI": 0,
    "Monday Effect": 0,
    "RSI(2) Mean Reversion": 0,
    "TGA-Follow": 0,
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


MAX_ALLOWED_DRAWDOWN_PCT = 0.06        # hard funded-account limit - breach means the account is lost
DRAWDOWN_WARNING_PCT = 0.04            # early warning threshold, well before the hard limit


def load_equity():
    if EQUITY_PATH.exists():
        return json.loads(EQUITY_PATH.read_text())["equity"]
    return STARTING_EQUITY


def load_peak_equity():
    if EQUITY_PATH.exists():
        data = json.loads(EQUITY_PATH.read_text())
        return data.get("peak_equity", data.get("equity", STARTING_EQUITY))
    return STARTING_EQUITY


def save_equity(equity):
    peak = max(load_peak_equity(), equity)
    EQUITY_PATH.write_text(json.dumps({"equity": equity, "starting_equity": STARTING_EQUITY, "peak_equity": peak}, indent=2))


def get_drawdown_status():
    """Returns (current_drawdown_pct, distance_to_limit_pct, warning_level)
    where warning_level is 'ok', 'warning', or 'BREACH'. Drawdown is
    measured against PEAK equity (the standard, correct way for a
    funded-account drawdown rule), not against starting capital -
    dropping from a new high is what counts, not just being below where
    you started."""
    equity = load_equity()
    peak = load_peak_equity()
    if peak <= 0:
        return 0.0, MAX_ALLOWED_DRAWDOWN_PCT, "ok"
    current_dd = (peak - equity) / peak
    distance_to_limit = MAX_ALLOWED_DRAWDOWN_PCT - current_dd
    if current_dd >= MAX_ALLOWED_DRAWDOWN_PCT:
        level = "BREACH"
    elif current_dd >= DRAWDOWN_WARNING_PCT:
        level = "warning"
    else:
        level = "ok"
    return current_dd, distance_to_limit, level


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
    if COT_STATE_PATH.exists():
        cot_full_state = json.loads(COT_STATE_PATH.read_text())
        cot_state = cot_full_state.get("cot", {})
        for inst_state in cot_state.values():
            if inst_state.get("state", 0) != 0:
                committed += get_risk_pct("COT Positioning Extreme")
    if VIXFADE_STATE_PATH.exists():
        vixfade_full_state = json.loads(VIXFADE_STATE_PATH.read_text())
        vixfade_state = vixfade_full_state.get("vixfade", {})
        if vixfade_state.get("state", 0) != 0:
            committed += get_risk_pct("VIX Shock-Fade")
    if TGAFOLLOW_STATE_PATH.exists():
        tgafollow_full_state = json.loads(TGAFOLLOW_STATE_PATH.read_text())
        tgafollow_state = tgafollow_full_state.get("tgafollow", {})
        for inst_state in tgafollow_state.values():
            if inst_state.get("state", 0) != 0:
                committed += get_risk_pct("TGA-Follow")
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
    since inception, drawdown status against the hard 6% funded-account
    limit, and today's closed trades specifically (from journal.csv,
    filtered to today's UTC date). Designed to be sent as its own
    Telegram message once a day, separate from each strategy's own
    per-signal messages."""
    equity = load_equity()
    peak = load_peak_equity()
    total_pnl = equity - STARTING_EQUITY
    total_pnl_pct = (equity / STARTING_EQUITY - 1) * 100
    current_dd, distance_to_limit, dd_level = get_drawdown_status()

    dd_flag = {"ok": "", "warning": " ⚠️ WARNING", "BREACH": " 🚨 LIMIT BREACHED"}[dd_level]

    lines = [
        "DAILY P&L SUMMARY",
        f"Current equity: £{equity:,.2f}",
        f"Peak equity: £{peak:,.2f}",
        f"Total P&L since inception: £{total_pnl:,.2f} ({total_pnl_pct:+.2f}%)",
        "",
        f"DRAWDOWN: {current_dd:.2%} of peak (limit: {MAX_ALLOWED_DRAWDOWN_PCT:.0%}){dd_flag}",
        f"Room remaining before limit: {distance_to_limit:.2%}",
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
