"""
Daily forward-test signal logger — NAS100 Pivot S/R (long-only, vol-scaled)
and EURGBP Donchian(20) (reversal), both with 1% risk-based journaling.

Run once per day after the daily close. See journal.py for the risk/R-
multiple methodology. Both strategies keep their ORIGINAL entry/exit
rules unchanged — the only addition here is an ATR(14) risk reference
at entry, used for position-sizing and journal purposes only.

Requires env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Data source: Yahoo Finance (^NDX for NAS100, EURGBP=X for EURGBP)
"""
import os
import json
import requests
import pandas as pd
import numpy as np
from pathlib import Path
from journal import record_trade_close, current_risk_gbp, load_equity, available_risk_fraction, get_risk_pct
from execution import execute_entry, execute_exit, EXECUTION_ENABLED

BASE = Path(__file__).parent
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

NAS100_LOG = BASE / "nas100_log.csv"
DONCHIAN_LOG = BASE / "donchian_log.csv"
DAILY_STATE_PATH = BASE / "daily_state.json"

MONDAY_LOG = BASE / "monday_effect_log.csv"

MONDAY_EFFECT_INSTRUMENTS = {
    'NAS100_USD': '^NDX', 'SPX500': '^GSPC', 'US30': '^DJI', 'US2000': '^RUT',
}
# Selected from the 34-instrument sweep: these 4 (all equity indices) showed
# PF 1.38-1.52 for the Monday effect, the strongest and cleanest of any
# instrument tested - see the book-strategy deep-dive audit. Randomization
# test 100th percentile, both IS (0.801) and OOS (2.040) Sharpe positive,
# cost-stress robust (PF 1.453->1.060 even at 5x cost), and importantly LOW
# correlation (0.26-0.40) with NAS100 Pivot/Donchian/Connors RSI - genuinely
# distinct, not redundant. Runs at standard 1% risk given its cost-stress
# margin, unlike the thinner-margin strategies at 0.5%.
# Mechanic: enter LONG at Friday's close, hold over the weekend, exit at
# the following Monday's close - matching exactly what was backtested
# (which measured Monday's close-to-close return, i.e. Friday-close to
# Monday-close).

CONNORS_LOG = BASE / "connors_log.csv"

CONNORS_INSTRUMENTS = {
    'SPX500': '^GSPC', 'CHFJPY': 'CHFJPY=X', 'US30': '^DJI', 'NAS100_USD': '^NDX',
    'XAUUSD': 'GC=F', 'DE30': '^GDAXI', 'USDJPY': 'USDJPY=X', 'EURJPY': 'EURJPY=X',
    'US2000': '^RUT', 'UK100': '^FTSE',
}
# Selected as the 10 instruments (of 34 backtested) showing profit factor > 1.08
# in the full validation - see the Connors RSI Composite deep-dive audit:
# mechanical audit clean, 100th-percentile randomization test, cost-stress
# robust (PF 1.05->1.04 at 5x cost, the most robust of 8 mean-reversion
# candidates tested), bootstrap worst-case drawdown only -1.9%, low
# correlation with Donchian (0.61) and NAS100 Pivot (0.51) and with the
# other 7 mean-reversion variants tested alongside it (0.2-0.5) - genuinely
# distinct signal, not redundant with what's already live.
# Runs at 0.5% risk (same precedent as NAS100 Pivot) given its overall
# margin is thin in absolute terms (backtested PF 1.02-1.14) even though
# unusually robust to cost stress specifically.

DONCHIAN_INSTRUMENTS = {
    'EURGBP': {'yahoo': 'EURGBP=X', 'variant': 'baseline'},
    'EURCAD': {'yahoo': 'EURCAD=X', 'variant': 'baseline'},
    'GBPUSD': {'yahoo': 'GBPUSD=X', 'variant': 'baseline'},
    'US30':   {'yahoo': '^DJI',     'variant': 'long_only'},
    'EURJPY': {'yahoo': 'EURJPY=X', 'variant': 'long_only'},
    'DE30':   {'yahoo': '^GDAXI',   'variant': 'long_only'},
    'GBPCAD': {'yahoo': 'GBPCAD=X', 'variant': 'trend_filter'},
    'XAUUSD':     {'yahoo': 'GC=F', 'variant': 'long_only'},
    'GBPCHF':     {'yahoo': 'GBPCHF=X', 'variant': 'trend_filter'},
    'NAS100_USD': {'yahoo': '^NDX',     'variant': 'long_only'},
    'SPX500':     {'yahoo': '^GSPC',    'variant': 'long_only'},
}
# Best-validated variant per instrument, from the extended Donchian sweep:
# baseline = plain reversal (no filters) — EURGBP, EURCAD, GBPUSD
# long_only = shorts dropped — US30, EURJPY, DE30, XAUUSD, NAS100_USD, SPX500
# trend_filter = only trade with a 100-day MA in agreement — GBPCAD, GBPCHF
#
# XAUUSD, GBPCHF, NAS100_USD, SPX500 added after an out-of-sample audit: the
# same 5-variant selection process was re-run on 21 previously-untouched
# instruments (none of which had been through variant selection before) to
# check whether the process itself replicates or was overfit to the original
# 7. Only 13/21 held up with consistent in-sample AND out-of-sample results;
# these 4 were the strongest of that batch. The other 17 were left out.
#
# NOTE: XAUUSD, NAS100_USD, and SPX500 already run under other strategies
# (ADX+Supertrend basket; NAS100 also runs the separate Pivot S/R system).
# This is intentional — different signals on the same instrument, tracked
# separately in the journal by strategy name, same as EURGBP already running
# both Donchian and ADX+Supertrend.


def fetch_latest_daily_bar(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "3mo", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"Yahoo error: {data}")
    result = result[0]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    closes, highs, lows = quote["close"], quote["high"], quote["low"]
    valid = [i for i in range(len(closes)) if closes[i] is not None]
    if len(valid) < 2:
        raise RuntimeError("Not enough confirmed daily bars")
    latest_i, prior_i = valid[-1], valid[-2]
    date = pd.to_datetime(timestamps[latest_i], unit="s").strftime("%Y-%m-%d")
    return {
        "date": date, "close": float(closes[latest_i]),
        "high": float(highs[latest_i]), "low": float(lows[latest_i]),
        "prior_high": float(highs[prior_i]), "prior_low": float(lows[prior_i]),
        "prior_close": float(closes[prior_i]),
    }


def load_price_log(path):
    if path.exists():
        return pd.read_csv(path, parse_dates=["date"])
    raise FileNotFoundError(f"{path} not found — seed it before first run.")


def load_daily_state():
    if DAILY_STATE_PATH.exists():
        return json.loads(DAILY_STATE_PATH.read_text())
    state = {"nas100": {"state": 0, "entry_price": None, "risk_ref": None, "vol_scale": 1.0, "trade_id": None}}
    state["donchian"] = {inst: {"state": 0, "entry_price": None, "risk_ref": None, "trade_id": None} for inst in DONCHIAN_INSTRUMENTS}
    return state


def atr14_from_log(log_df, high_col="high", low_col="low", close_col="close"):
    h, l, c = log_df[high_col], log_df[low_col], log_df[close_col]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(14).mean().iloc[-1]


def process_nas100(state, msgs, open_counter):
    log = load_price_log(NAS100_LOG)
    bar = fetch_latest_daily_bar("^NDX")
    if pd.to_datetime(bar["date"]) in set(log["date"]):
        msgs.append("NAS100: already logged today.")
        return state, log

    pivot = (bar["prior_high"] + bar["prior_low"] + bar["prior_close"]) / 3
    resistance = 2 * pivot - bar["prior_low"]
    close = bar["close"]

    row = {"date": pd.to_datetime(bar["date"]), "close": close, "high": bar["high"], "low": bar["low"],
           "pivot": pivot, "resistance": resistance}
    log = pd.concat([log, pd.DataFrame([row])], ignore_index=True)

    realized_vol = log["close"].pct_change().rolling(20).std().iloc[-1]
    median_vol = log["close"].pct_change().rolling(20).std().rolling(500).median().iloc[-1]
    vol_scale = np.clip(median_vol / realized_vol, 0.3, 2.0) if realized_vol and not np.isnan(realized_vol) else 1.0

    s = state["nas100"]
    prev_pos = s["state"]

    if prev_pos == 0 and close > pivot:
        risk_frac = available_risk_fraction("Pivot S/R", open_counter[0])
        if risk_frac <= 0:
            msgs.append(f"NAS100: signal fired (close > pivot) but SKIPPED — 10% total risk budget already full.")
        else:
            atr = atr14_from_log(log)
            risk_ref = close - atr if not np.isnan(atr) else close * 0.99
            vol_scaled_risk_gbp = current_risk_gbp("Pivot S/R", risk_frac) * vol_scale
            fill = execute_entry("NAS100_USD", "long", vol_scaled_risk_gbp, risk_ref)
            trade_id = fill["trade_id"] if fill else None
            s.update({"state": 1, "entry_price": close, "risk_ref": risk_ref, "vol_scale": vol_scale, "trade_id": trade_id, "risk_fraction": risk_frac})
            open_counter[0] += get_risk_pct("Pivot S/R")
            exec_note = f" [LIVE, trade {trade_id}]" if fill else (" [EXECUTION ENABLED but order failed]" if EXECUTION_ENABLED else "")
            frac_note = f" [sized to {risk_frac:.0%} of full slice, risk budget partially used]" if risk_frac < 1.0 else ""
            msgs.append(f"*NAS100* — ENTER LONG @ {close:.1f} (risk ref {risk_ref:.1f}, ~£{vol_scaled_risk_gbp:,.0f} at risk incl. {vol_scale:.2f}x vol-scale){exec_note}{frac_note}")
    elif prev_pos == 1 and close >= resistance:
        if s.get("trade_id"):
            execute_exit(s["trade_id"])
        pnl, new_equity = record_trade_close("NAS100", "Pivot S/R", "long", s["entry_price"], s["risk_ref"], close, s.get("risk_fraction", 1.0))
        pnl *= s.get("vol_scale", 1.0)
        msgs.append(f"*NAS100* — EXIT LONG @ {close:.1f} (hit resistance). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
        s.update({"state": 0, "entry_price": None, "risk_ref": None, "vol_scale": 1.0, "trade_id": None, "risk_fraction": 1.0})
    else:
        action = "HOLD LONG" if prev_pos == 1 else "FLAT"
        msgs.append(f"NAS100: {action} @ {close:.1f} (pivot {pivot:.1f}, resistance {resistance:.1f})")

    state["nas100"] = s
    return state, log


def rsi_series(closes, n):
    """Standard RSI, causal by construction (rolling mean of gains/losses).
    Deliberately NOT special-cased for zero-loss/zero-gain windows (which
    would naively produce NaN here) - the backtest that validated this
    strategy used this exact naive formula, and its outer .ffill() treated
    those NaN moments as 'no new signal today, stay sticky' rather than a
    special RSI=100/0 case. Matching that exactly (see compute_connors_rsi)
    preserves fidelity with what was actually tested - inventing a more
    'correct' RSI convention here would make live behavior diverge from
    the validated backtest on these (rare) days."""
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = -delta.clip(upper=0).rolling(n).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_connors_rsi(inst_log, rsi_n=3, streak_n=2, rank_n=100):
    """Connors RSI composite: average of (RSI of price, RSI of the up/down
    streak length, percentile rank of today's return vs trailing rank_n
    days). Matches the backtested/validated construction exactly - see
    strategy_catalog deep-dive.

    Returns a tuple (has_enough_history: bool, composite: float or None).
    composite is None either because there isn't enough history yet
    (has_enough_history=False), or because an RSI component hit a
    transient zero-gain/zero-loss window and is NaN today
    (has_enough_history=True, composite=None) - the backtest's .ffill()
    treated that second case as 'no new signal today, stay sticky', which
    the caller must replicate exactly, not as 'still building history'."""
    closes = inst_log["close"].reset_index(drop=True)
    if len(closes) < rank_n + rsi_n + 5:
        return False, None
    price_rsi = rsi_series(closes, rsi_n).iloc[-1]
    updown = np.sign(closes.diff()).fillna(0)
    streak = updown.groupby((updown != updown.shift()).cumsum()).cumcount() + 1
    streak = streak * updown
    streak_rsi = rsi_series(streak, streak_n).iloc[-1]
    ret = closes.pct_change()
    recent = ret.tail(rank_n + 1)
    rank_pct = (recent.iloc[:-1] < recent.iloc[-1]).mean() * 100 if len(recent) > 1 else 50.0
    if np.isnan(price_rsi) or np.isnan(streak_rsi):
        return True, None
    return True, (price_rsi + streak_rsi + rank_pct) / 3


def process_monday_effect_all(state, msgs, open_counter):
    log = load_price_log(MONDAY_LOG)
    monday_state = state.get("monday_effect", {})

    for inst, yahoo_symbol in MONDAY_EFFECT_INSTRUMENTS.items():
        bar = fetch_latest_daily_bar(yahoo_symbol)
        inst_log = log[log["instrument"] == inst]
        bar_date = pd.to_datetime(bar["date"])

        if bar_date in set(inst_log["date"]):
            msgs.append(f"{inst}: already logged today.")
            continue

        close = bar["close"]
        row = {"instrument": inst, "date": bar_date, "close": close}
        log = pd.concat([log, pd.DataFrame([row])], ignore_index=True)

        s = monday_state.get(inst, {"state": 0, "entry_price": None, "risk_ref": None, "trade_id": None, "risk_fraction": 1.0})
        weekday = bar_date.dayofweek  # Monday=0, ..., Friday=4

        # === EXIT: if a position is open (entered last Friday) and today
        # is Monday, close it at today's close - matching exactly what was
        # backtested (Friday-close to Monday-close return).
        if s["state"] == 1 and weekday == 0:
            if s.get("trade_id"):
                execute_exit(s["trade_id"])
            pnl, new_equity = record_trade_close(inst, "Monday Effect", "long", s["entry_price"], s["risk_ref"], close, s.get("risk_fraction", 1.0))
            msgs.append(f"*{inst}* — EXIT LONG @ {close:.5f} (Monday Effect). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
            s.update({"state": 0, "entry_price": None, "risk_ref": None, "trade_id": None, "risk_fraction": 1.0})

        # === ENTRY: only on Friday, only if currently flat.
        elif s["state"] == 0 and weekday == 4:
            risk_frac = available_risk_fraction("Monday Effect", open_counter[0])
            if risk_frac <= 0:
                msgs.append(f"{inst}: Friday entry signal but SKIPPED — 10% risk budget full.")
            else:
                risk_ref = close * 0.99  # 1% nominal stop-distance for position sizing purposes only; this strategy has no real stop, it exits Monday regardless
                risk_gbp = current_risk_gbp("Monday Effect", risk_frac)
                fill = execute_entry(inst, "long", risk_gbp, risk_ref)
                trade_id = fill["trade_id"] if fill else None
                s.update({"state": 1, "entry_price": close, "risk_ref": risk_ref, "trade_id": trade_id, "risk_fraction": risk_frac})
                open_counter[0] += get_risk_pct("Monday Effect")
                exec_note = f" [LIVE, trade {trade_id}]" if fill else (" [EXECUTION ENABLED but order failed]" if EXECUTION_ENABLED else "")
                frac_note = f" [{risk_frac:.0%} of full slice]" if risk_frac < 1.0 else ""
                msgs.append(f"*{inst}* — ENTER LONG @ {close:.5f} (Monday Effect, Friday hold-over-weekend) (~£{risk_gbp:,.0f} at risk){exec_note}{frac_note}")
        else:
            action = "HOLD (over weekend)" if s["state"] == 1 else "FLAT (not Friday)"
            msgs.append(f"{inst} (Monday Effect): {action} @ {close:.5f}")

        monday_state[inst] = s

    state["monday_effect"] = monday_state
    return state, log


def process_connors_all(state, msgs, open_counter, low_th=15, high_th=85, stop_atr_mult=2.0):
    log = load_price_log(CONNORS_LOG)
    connors_state = state.get("connors", {})

    for inst, yahoo_symbol in CONNORS_INSTRUMENTS.items():
        bar = fetch_latest_daily_bar(yahoo_symbol)
        inst_log = log[log["instrument"] == inst]

        if pd.to_datetime(bar["date"]) in set(inst_log["date"]):
            msgs.append(f"{inst}: already logged today.")
            continue

        close, high, low = bar["close"], bar["high"], bar["low"]
        row = {"instrument": inst, "date": pd.to_datetime(bar["date"]), "close": close, "high": high, "low": low}
        log = pd.concat([log, pd.DataFrame([row])], ignore_index=True)
        inst_log = log[log["instrument"] == inst]

        has_history, composite = compute_connors_rsi(inst_log)
        s = connors_state.get(inst, {"state": 0, "entry_price": None, "risk_ref": None, "trade_id": None,
                                      "risk_fraction": 1.0, "stop_price": None, "best_price": None})
        prev_pos = s["state"]

        if not has_history:
            msgs.append(f"{inst} (Connors RSI): building history, not enough data yet.")
            connors_state[inst] = s
            continue

        atr = atr14_from_log(inst_log)
        stopped_out = False

        # === STEP 1: check the trailing stop FIRST, using the level set
        # BEFORE today (from yesterday's ATR and best-price-so-far) - never
        # a level computed from today's own high/low. Only after this check
        # does the stop get allowed to ratchet, using today's now-complete
        # data, for tomorrow's check. Same causally-correct sequencing
        # already validated for QM Structural Rebuild and re-confirmed in
        # the fresh backtest that justified adding this stop at all.
        if prev_pos == 1 and s.get("stop_price") is not None:
            if low <= s["stop_price"]:
                stopped_out = True
                if s.get("trade_id"):
                    execute_exit(s["trade_id"])
                pnl, new_equity = record_trade_close(inst, "Connors RSI", "long", s["entry_price"], s["risk_ref"], s["stop_price"], s.get("risk_fraction", 1.0))
                msgs.append(f"*{inst}* — STOPPED OUT of LONG @ {s['stop_price']:.5f} (Connors RSI trailing stop). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
                s.update({"state": 0, "entry_price": None, "risk_ref": None, "trade_id": None, "risk_fraction": 1.0, "stop_price": None, "best_price": None})
                prev_pos = 0
        elif prev_pos == -1 and s.get("stop_price") is not None:
            if high >= s["stop_price"]:
                stopped_out = True
                if s.get("trade_id"):
                    execute_exit(s["trade_id"])
                pnl, new_equity = record_trade_close(inst, "Connors RSI", "short", s["entry_price"], s["risk_ref"], s["stop_price"], s.get("risk_fraction", 1.0))
                msgs.append(f"*{inst}* — STOPPED OUT of SHORT @ {s['stop_price']:.5f} (Connors RSI trailing stop). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
                s.update({"state": 0, "entry_price": None, "risk_ref": None, "trade_id": None, "risk_fraction": 1.0, "stop_price": None, "best_price": None})
                prev_pos = 0

        if stopped_out:
            connors_state[inst] = s
            continue

        if composite is None:
            # Transient RSI edge case - matches backtest's .ffill(): no new
            # signal today, position stays exactly as it was, but the
            # trailing stop (if any) still ratchets below using today's data.
            if prev_pos == 1 and not np.isnan(atr):
                s["best_price"] = max(s["best_price"], high)
                new_stop = s["best_price"] - stop_atr_mult*atr
                if new_stop > s["stop_price"]: s["stop_price"] = new_stop
            elif prev_pos == -1 and not np.isnan(atr):
                s["best_price"] = min(s["best_price"], low)
                new_stop = s["best_price"] + stop_atr_mult*atr
                if new_stop < s["stop_price"]: s["stop_price"] = new_stop
            action = {1: "HOLD LONG", -1: "HOLD SHORT", 0: "FLAT"}[prev_pos]
            msgs.append(f"{inst} (Connors RSI=n/a today): {action} @ {close:.5f}")
            connors_state[inst] = s
            continue

        # STICKY signal, matching the backtested/validated logic exactly:
        # position only changes on a fresh extreme (composite<low_th or
        # >high_th) - it does NOT auto-flatten in the neutral zone, it
        # carries forward from the last extreme signal until the opposite
        # extreme fires OR the trailing stop (checked above) is hit.
        if composite < low_th and prev_pos != 1:
            if prev_pos == -1:
                if s.get("trade_id"):
                    execute_exit(s["trade_id"])
                pnl, new_equity = record_trade_close(inst, "Connors RSI", "short", s["entry_price"], s["risk_ref"], close, s.get("risk_fraction", 1.0))
                msgs.append(f"*{inst}* — EXIT SHORT @ {close:.5f} (Connors RSI). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
            risk_frac = available_risk_fraction("Connors RSI", open_counter[0])
            if risk_frac <= 0:
                msgs.append(f"{inst}: LONG signal fired (Connors RSI={composite:.1f}) but SKIPPED — 10% risk budget full.")
                s.update({"state": 0, "entry_price": None, "risk_ref": None, "trade_id": None, "risk_fraction": 1.0, "stop_price": None, "best_price": None})
            else:
                risk_ref = close - atr if not np.isnan(atr) else close * 0.99
                risk_gbp = current_risk_gbp("Connors RSI", risk_frac)
                fill = execute_entry(inst, "long", risk_gbp, risk_ref)
                trade_id = fill["trade_id"] if fill else None
                initial_stop = close - stop_atr_mult*atr if not np.isnan(atr) else close*0.97
                s.update({"state": 1, "entry_price": close, "risk_ref": risk_ref, "trade_id": trade_id, "risk_fraction": risk_frac,
                          "stop_price": initial_stop, "best_price": close})
                open_counter[0] += get_risk_pct("Connors RSI")
                exec_note = f" [LIVE, trade {trade_id}]" if fill else (" [EXECUTION ENABLED but order failed]" if EXECUTION_ENABLED else "")
                frac_note = f" [{risk_frac:.0%} of full slice]" if risk_frac < 1.0 else ""
                msgs.append(f"*{inst}* (Connors RSI={composite:.1f}) — ENTER LONG @ {close:.5f}, trailing stop {initial_stop:.5f} (~£{risk_gbp:,.0f} at risk){exec_note}{frac_note}")
        elif composite > high_th and prev_pos != -1:
            if prev_pos == 1:
                if s.get("trade_id"):
                    execute_exit(s["trade_id"])
                pnl, new_equity = record_trade_close(inst, "Connors RSI", "long", s["entry_price"], s["risk_ref"], close, s.get("risk_fraction", 1.0))
                msgs.append(f"*{inst}* — EXIT LONG @ {close:.5f} (Connors RSI). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
            risk_frac = available_risk_fraction("Connors RSI", open_counter[0])
            if risk_frac <= 0:
                msgs.append(f"{inst}: SHORT signal fired (Connors RSI={composite:.1f}) but SKIPPED — 10% risk budget full.")
                s.update({"state": 0, "entry_price": None, "risk_ref": None, "trade_id": None, "risk_fraction": 1.0, "stop_price": None, "best_price": None})
            else:
                risk_ref = close + atr if not np.isnan(atr) else close * 1.01
                risk_gbp = current_risk_gbp("Connors RSI", risk_frac)
                fill = execute_entry(inst, "short", risk_gbp, risk_ref)
                trade_id = fill["trade_id"] if fill else None
                initial_stop = close + stop_atr_mult*atr if not np.isnan(atr) else close*1.03
                s.update({"state": -1, "entry_price": close, "risk_ref": risk_ref, "trade_id": trade_id, "risk_fraction": risk_frac,
                          "stop_price": initial_stop, "best_price": close})
                open_counter[0] += get_risk_pct("Connors RSI")
                exec_note = f" [LIVE, trade {trade_id}]" if fill else (" [EXECUTION ENABLED but order failed]" if EXECUTION_ENABLED else "")
                frac_note = f" [{risk_frac:.0%} of full slice]" if risk_frac < 1.0 else ""
                msgs.append(f"*{inst}* (Connors RSI={composite:.1f}) — ENTER SHORT @ {close:.5f}, trailing stop {initial_stop:.5f} (~£{risk_gbp:,.0f} at risk){exec_note}{frac_note}")
        else:
            # holding (or flat) with no new extreme signal - ratchet the
            # trailing stop using TODAY's now-complete data, for TOMORROW's
            # check only (never re-checked against today's own range again).
            if prev_pos == 1 and not np.isnan(atr):
                s["best_price"] = max(s["best_price"], high)
                new_stop = s["best_price"] - stop_atr_mult*atr
                if new_stop > s["stop_price"]: s["stop_price"] = new_stop
            elif prev_pos == -1 and not np.isnan(atr):
                s["best_price"] = min(s["best_price"], low)
                new_stop = s["best_price"] + stop_atr_mult*atr
                if new_stop < s["stop_price"]: s["stop_price"] = new_stop
            action = {1: "HOLD LONG", -1: "HOLD SHORT", 0: "FLAT"}[prev_pos]
            stop_note = f", stop {s['stop_price']:.5f}" if prev_pos != 0 and s.get("stop_price") else ""
            msgs.append(f"{inst} (Connors RSI={composite:.1f}): {action} @ {close:.5f}{stop_note}")

        connors_state[inst] = s

    state["connors"] = connors_state
    return state, log


def process_donchian_all(state, msgs, open_counter):
    log = load_price_log(DONCHIAN_LOG)
    donchian_state = state.get("donchian", {})

    for inst, cfg in DONCHIAN_INSTRUMENTS.items():
        variant = cfg["variant"]
        bar = fetch_latest_daily_bar(cfg["yahoo"])
        inst_log = log[log["instrument"] == inst]

        if pd.to_datetime(bar["date"]) in set(inst_log["date"]):
            msgs.append(f"{inst}: already logged today.")
            continue

        close = bar["close"]
        row = {"instrument": inst, "date": pd.to_datetime(bar["date"]), "close": close, "high": bar["high"], "low": bar["low"]}
        log = pd.concat([log, pd.DataFrame([row])], ignore_index=True)
        inst_log = log[log["instrument"] == inst]  # refresh after append

        ceiling = inst_log["close"].tail(20).max()
        floor = inst_log["close"].tail(20).min()

        s = donchian_state.get(inst, {"state": 0, "entry_price": None, "risk_ref": None, "trade_id": None, "risk_fraction": 1.0})
        prev_pos = s["state"]

        # trend_filter variant needs a 100-day MA gate on entries
        ma_ok_long = ma_ok_short = True
        if variant == "trend_filter":
            ma100 = inst_log["close"].tail(100).mean()
            ma_ok_long = close < ma100
            ma_ok_short = close > ma100

        can_short = (variant in ("baseline", "trend_filter")) and (variant != "trend_filter" or ma_ok_short)
        can_long_entry = (variant != "trend_filter") or ma_ok_long

        if close >= ceiling and can_short:
            if prev_pos == 1:
                if s.get("trade_id"):
                    execute_exit(s["trade_id"])
                pnl, new_equity = record_trade_close(inst, "Donchian(20)", "long", s["entry_price"], s["risk_ref"], close, s.get("risk_fraction", 1.0))
                msgs.append(f"*{inst}* — EXIT LONG @ {close:.5f}. P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
            risk_frac = available_risk_fraction("Donchian(20)", open_counter[0])
            if risk_frac <= 0:
                msgs.append(f"{inst}: SHORT signal fired but SKIPPED — 10% total risk budget already full.")
                s.update({"state": 0, "entry_price": None, "risk_ref": None, "trade_id": None, "risk_fraction": 1.0})
            else:
                atr = atr14_from_log(inst_log)
                risk_ref = close + atr if not np.isnan(atr) else close * 1.01
                risk_gbp = current_risk_gbp("Donchian(20)", risk_frac)
                fill = execute_entry(inst, "short", risk_gbp, risk_ref)
                trade_id = fill["trade_id"] if fill else None
                s.update({"state": -1, "entry_price": close, "risk_ref": risk_ref, "trade_id": trade_id, "risk_fraction": risk_frac})
                open_counter[0] += get_risk_pct("Donchian(20)")
                exec_note = f" [LIVE, trade {trade_id}]" if fill else (" [EXECUTION ENABLED but order failed]" if EXECUTION_ENABLED else "")
                frac_note = f" [{risk_frac:.0%} of full slice]" if risk_frac < 1.0 else ""
                msgs.append(f"*{inst}* ({variant}) — ENTER SHORT @ {close:.5f} (~£{risk_gbp:,.0f} at risk){exec_note}{frac_note}")
        elif close >= ceiling and prev_pos == 1 and variant == "long_only":
            # long-only variant: still exit the long on ceiling touch, just don't flip short
            if s.get("trade_id"):
                execute_exit(s["trade_id"])
            pnl, new_equity = record_trade_close(inst, "Donchian(20)", "long", s["entry_price"], s["risk_ref"], close, s.get("risk_fraction", 1.0))
            msgs.append(f"*{inst}* — EXIT LONG @ {close:.5f} (long-only, no short taken). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
            s.update({"state": 0, "entry_price": None, "risk_ref": None, "trade_id": None, "risk_fraction": 1.0})
        elif close <= floor and can_long_entry:
            if prev_pos == -1:
                if s.get("trade_id"):
                    execute_exit(s["trade_id"])
                pnl, new_equity = record_trade_close(inst, "Donchian(20)", "short", s["entry_price"], s["risk_ref"], close, s.get("risk_fraction", 1.0))
                msgs.append(f"*{inst}* — EXIT SHORT @ {close:.5f}. P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
            risk_frac = available_risk_fraction("Donchian(20)", open_counter[0])
            if risk_frac <= 0:
                msgs.append(f"{inst}: LONG signal fired but SKIPPED — 10% total risk budget already full.")
                s.update({"state": 0, "entry_price": None, "risk_ref": None, "trade_id": None, "risk_fraction": 1.0})
            else:
                atr = atr14_from_log(inst_log)
                risk_ref = close - atr if not np.isnan(atr) else close * 0.99
                risk_gbp = current_risk_gbp("Donchian(20)", risk_frac)
                fill = execute_entry(inst, "long", risk_gbp, risk_ref)
                trade_id = fill["trade_id"] if fill else None
                s.update({"state": 1, "entry_price": close, "risk_ref": risk_ref, "trade_id": trade_id, "risk_fraction": risk_frac})
                open_counter[0] += get_risk_pct("Donchian(20)")
                exec_note = f" [LIVE, trade {trade_id}]" if fill else (" [EXECUTION ENABLED but order failed]" if EXECUTION_ENABLED else "")
                frac_note = f" [{risk_frac:.0%} of full slice]" if risk_frac < 1.0 else ""
                msgs.append(f"*{inst}* ({variant}) — ENTER LONG @ {close:.5f} (~£{risk_gbp:,.0f} at risk){exec_note}{frac_note}")
        else:
            action = {1: "HOLD LONG", -1: "HOLD SHORT", 0: "FLAT"}[prev_pos]
            msgs.append(f"{inst} ({variant}): {action} @ {close:.5f} (ceiling {ceiling:.5f}, floor {floor:.5f})")

        donchian_state[inst] = s

    state["donchian"] = donchian_state
    return state, log


def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[warn] Telegram not configured:\n" + msg)
        return
    # Plain text, no parse_mode — Telegram's legacy Markdown parser treats
    # single underscores as italic delimiters, and instrument names like
    # NAS100_USD contain an unpaired underscore. That silently breaks the
    # ENTIRE message (Telegram rejects malformed entities outright), which
    # is exactly what happened here — the message was built correctly but
    # never delivered, with no visible error anywhere. Plain text removes
    # this whole class of failure. Bold/italic markers (*text*) are left
    # in the message body as plain asterisks — readable, just not styled.
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TG_CHAT_ID, "text": msg}, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] Telegram send failed: HTTP {r.status_code} — {r.text}")
        else:
            print("[ok] Telegram message sent successfully.")
    except Exception as e:
        print(f"[ERROR] Telegram send raised an exception: {e}")


def main():
    if EXECUTION_ENABLED:
        import oanda_client as oanda
        env_label = f"OANDA {oanda.ENVIRONMENT.upper()}"
        try:
            live_balance = oanda.get_current_balance()
            from journal import save_equity
            save_equity(live_balance)
            print(f"[{env_label}] Synced equity to live account balance: £{live_balance:,.2f}")
        except Exception as e:
            print(f"[warn] Could not sync live balance, using journal's tracked figure instead: {e}")
    else:
        print("[forward-test mode] OANDA credentials not set — signal logging only, no real orders.")

    state = load_daily_state()
    msgs = []
    open_counter = [0]  # tracks new entries opened earlier in this same run, see available_risk_fraction()

    state, nas100_log = process_nas100(state, msgs, open_counter)
    state, donchian_log = process_donchian_all(state, msgs, open_counter)
    state, connors_log = process_connors_all(state, msgs, open_counter)
    state, monday_log = process_monday_effect_all(state, msgs, open_counter)

    nas100_log.to_csv(NAS100_LOG, index=False)
    # trim donchian log per-instrument to last 600 rows to keep file size sane
    trimmed = [donchian_log[donchian_log["instrument"] == inst].sort_values("date").tail(600) for inst in DONCHIAN_INSTRUMENTS]
    donchian_log = pd.concat(trimmed, ignore_index=True)
    donchian_log.to_csv(DONCHIAN_LOG, index=False)
    # Connors RSI needs 100+ days of history for its rank component - keep 400 rows of headroom
    trimmed_connors = [connors_log[connors_log["instrument"] == inst].sort_values("date").tail(400) for inst in CONNORS_INSTRUMENTS]
    connors_log = pd.concat(trimmed_connors, ignore_index=True)
    connors_log.to_csv(CONNORS_LOG, index=False)
    trimmed_monday = [monday_log[monday_log["instrument"] == inst].sort_values("date").tail(30) for inst in MONDAY_EFFECT_INSTRUMENTS]
    monday_log = pd.concat(trimmed_monday, ignore_index=True)
    monday_log.to_csv(MONDAY_LOG, index=False)
    DAILY_STATE_PATH.write_text(json.dumps(state, indent=2))

    equity = load_equity()
    env_tag = "[LIVE TRADING]" if EXECUTION_ENABLED else "[forward-test / signal only]"
    header = f"*Daily Signals {env_tag} — Equity: £{equity:,.0f}*\n\n"
    full_msg = header + "\n".join(msgs)
    send_telegram(full_msg)
    print(full_msg)


if __name__ == "__main__":
    main()
