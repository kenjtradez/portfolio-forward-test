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
from journal import record_trade_close, current_risk_gbp, load_equity
from execution import execute_entry, execute_exit, EXECUTION_ENABLED

BASE = Path(__file__).parent
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

NAS100_LOG = BASE / "nas100_log.csv"
DONCHIAN_LOG = BASE / "donchian_log.csv"
DAILY_STATE_PATH = BASE / "daily_state.json"

DONCHIAN_INSTRUMENTS = {
    'EURGBP': {'yahoo': 'EURGBP=X', 'variant': 'baseline'},
    'EURCAD': {'yahoo': 'EURCAD=X', 'variant': 'baseline'},
    'GBPUSD': {'yahoo': 'GBPUSD=X', 'variant': 'baseline'},
    'US30':   {'yahoo': '^DJI',     'variant': 'long_only'},
    'EURJPY': {'yahoo': 'EURJPY=X', 'variant': 'long_only'},
    'DE30':   {'yahoo': '^GDAXI',   'variant': 'long_only'},
    'GBPCAD': {'yahoo': 'GBPCAD=X', 'variant': 'trend_filter'},
}
# Best-validated variant per instrument, from the extended Donchian sweep:
# baseline = plain reversal (no filters) — EURGBP, EURCAD, GBPUSD
# long_only = shorts dropped — US30, EURJPY, DE30
# trend_filter = only trade with a 100-day MA in agreement — GBPCAD


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


def process_nas100(state, msgs):
    log = load_price_log(NAS100_LOG)
    bar = fetch_latest_daily_bar("^NDX")
    if pd.to_datetime(bar["date"]) in set(log["date"]):
        msgs.append("NAS100: already logged today.")
        return state, log

    pivot = (bar["prior_high"] + bar["prior_low"] + bar["prior_close"]) / 3
    resistance = 2 * pivot - bar["prior_low"]
    close = bar["close"]

    row = {"date": bar["date"], "close": close, "high": bar["high"], "low": bar["low"],
           "pivot": pivot, "resistance": resistance}
    log = pd.concat([log, pd.DataFrame([row])], ignore_index=True)

    realized_vol = log["close"].pct_change().rolling(20).std().iloc[-1]
    median_vol = log["close"].pct_change().rolling(20).std().rolling(500).median().iloc[-1]
    vol_scale = np.clip(median_vol / realized_vol, 0.3, 2.0) if realized_vol and not np.isnan(realized_vol) else 1.0

    s = state["nas100"]
    prev_pos = s["state"]

    if prev_pos == 0 and close > pivot:
        atr = atr14_from_log(log)
        risk_ref = close - atr if not np.isnan(atr) else close * 0.99
        vol_scaled_risk_gbp = current_risk_gbp() * vol_scale
        # NOTE: the backtested strategy has no hard stop (exits on target/reversal
        # only) — for LIVE trading we attach risk_ref as a real protective stop
        # anyway. An unprotected live position is a bad idea regardless of what
        # the backtest assumed; this is a deliberate safety addition, not a
        # change to the tested edge (the target-based exit still fires first
        # in the normal case).
        fill = execute_entry("NAS100_USD", "long", vol_scaled_risk_gbp, risk_ref)
        trade_id = fill["trade_id"] if fill else None
        s.update({"state": 1, "entry_price": close, "risk_ref": risk_ref, "vol_scale": vol_scale, "trade_id": trade_id})
        exec_note = f" [LIVE, trade {trade_id}]" if fill else (" [EXECUTION ENABLED but order failed]" if EXECUTION_ENABLED else "")
        msgs.append(f"*NAS100* — ENTER LONG @ {close:.1f} (risk ref {risk_ref:.1f}, ~£{vol_scaled_risk_gbp:,.0f} at risk incl. {vol_scale:.2f}x vol-scale){exec_note}")
    elif prev_pos == 1 and close >= resistance:
        if s.get("trade_id"):
            execute_exit(s["trade_id"])
        pnl, new_equity = record_trade_close("NAS100", "Pivot S/R", "long", s["entry_price"], s["risk_ref"], close)
        pnl *= s.get("vol_scale", 1.0)
        msgs.append(f"*NAS100* — EXIT LONG @ {close:.1f} (hit resistance). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
        s.update({"state": 0, "entry_price": None, "risk_ref": None, "vol_scale": 1.0, "trade_id": None})
    else:
        action = "HOLD LONG" if prev_pos == 1 else "FLAT"
        msgs.append(f"NAS100: {action} @ {close:.1f} (pivot {pivot:.1f}, resistance {resistance:.1f})")

    state["nas100"] = s
    return state, log


def process_donchian_all(state, msgs):
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
        row = {"instrument": inst, "date": bar["date"], "close": close, "high": bar["high"], "low": bar["low"]}
        log = pd.concat([log, pd.DataFrame([row])], ignore_index=True)
        inst_log = log[log["instrument"] == inst]  # refresh after append

        ceiling = inst_log["close"].tail(20).max()
        floor = inst_log["close"].tail(20).min()

        s = donchian_state.get(inst, {"state": 0, "entry_price": None, "risk_ref": None, "trade_id": None})
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
                pnl, new_equity = record_trade_close(inst, "Donchian(20)", "long", s["entry_price"], s["risk_ref"], close)
                msgs.append(f"*{inst}* — EXIT LONG @ {close:.5f}. P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
            atr = atr14_from_log(inst_log)
            risk_ref = close + atr if not np.isnan(atr) else close * 1.01
            risk_gbp = current_risk_gbp()
            fill = execute_entry(inst, "short", risk_gbp, risk_ref)
            trade_id = fill["trade_id"] if fill else None
            s.update({"state": -1, "entry_price": close, "risk_ref": risk_ref, "trade_id": trade_id})
            exec_note = f" [LIVE, trade {trade_id}]" if fill else (" [EXECUTION ENABLED but order failed]" if EXECUTION_ENABLED else "")
            msgs.append(f"*{inst}* ({variant}) — ENTER SHORT @ {close:.5f} (~£{risk_gbp:,.0f} at risk){exec_note}")
        elif close >= ceiling and prev_pos == 1 and variant == "long_only":
            # long-only variant: still exit the long on ceiling touch, just don't flip short
            if s.get("trade_id"):
                execute_exit(s["trade_id"])
            pnl, new_equity = record_trade_close(inst, "Donchian(20)", "long", s["entry_price"], s["risk_ref"], close)
            msgs.append(f"*{inst}* — EXIT LONG @ {close:.5f} (long-only, no short taken). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
            s.update({"state": 0, "entry_price": None, "risk_ref": None, "trade_id": None})
        elif close <= floor and can_long_entry:
            if prev_pos == -1:
                if s.get("trade_id"):
                    execute_exit(s["trade_id"])
                pnl, new_equity = record_trade_close(inst, "Donchian(20)", "short", s["entry_price"], s["risk_ref"], close)
                msgs.append(f"*{inst}* — EXIT SHORT @ {close:.5f}. P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
            atr = atr14_from_log(inst_log)
            risk_ref = close - atr if not np.isnan(atr) else close * 0.99
            risk_gbp = current_risk_gbp()
            fill = execute_entry(inst, "long", risk_gbp, risk_ref)
            trade_id = fill["trade_id"] if fill else None
            s.update({"state": 1, "entry_price": close, "risk_ref": risk_ref, "trade_id": trade_id})
            exec_note = f" [LIVE, trade {trade_id}]" if fill else (" [EXECUTION ENABLED but order failed]" if EXECUTION_ENABLED else "")
            msgs.append(f"*{inst}* ({variant}) — ENTER LONG @ {close:.5f} (~£{risk_gbp:,.0f} at risk){exec_note}")
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
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"})


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

    state, nas100_log = process_nas100(state, msgs)
    state, donchian_log = process_donchian_all(state, msgs)

    nas100_log.to_csv(NAS100_LOG, index=False)
    # trim donchian log per-instrument to last 600 rows to keep file size sane
    trimmed = [donchian_log[donchian_log["instrument"] == inst].sort_values("date").tail(600) for inst in DONCHIAN_INSTRUMENTS]
    donchian_log = pd.concat(trimmed, ignore_index=True)
    donchian_log.to_csv(DONCHIAN_LOG, index=False)
    DAILY_STATE_PATH.write_text(json.dumps(state, indent=2))

    equity = load_equity()
    env_tag = "[LIVE TRADING]" if EXECUTION_ENABLED else "[forward-test / signal only]"
    header = f"*Daily Signals {env_tag} — Equity: £{equity:,.0f}*\n\n"
    full_msg = header + "\n".join(msgs)
    send_telegram(full_msg)
    print(full_msg)


if __name__ == "__main__":
    main()
