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

BASE = Path(__file__).parent
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

NAS100_LOG = BASE / "nas100_log.csv"
EURGBP_LOG = BASE / "eurgbp_log.csv"
DAILY_STATE_PATH = BASE / "daily_state.json"


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
    return {
        "nas100": {"state": 0, "entry_price": None, "risk_ref": None, "vol_scale": 1.0},
        "eurgbp": {"state": 0, "entry_price": None, "risk_ref": None},
    }


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
        s.update({"state": 1, "entry_price": close, "risk_ref": risk_ref, "vol_scale": vol_scale})
        risk_gbp = current_risk_gbp() * vol_scale
        msgs.append(f"*NAS100* — ENTER LONG @ {close:.1f} (risk ref {risk_ref:.1f}, ~£{risk_gbp:,.0f} at risk incl. {vol_scale:.2f}x vol-scale)")
    elif prev_pos == 1 and close >= resistance:
        pnl, new_equity = record_trade_close("NAS100", "Pivot S/R", "long", s["entry_price"], s["risk_ref"], close)
        pnl *= s.get("vol_scale", 1.0)
        msgs.append(f"*NAS100* — EXIT LONG @ {close:.1f} (hit resistance). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
        s.update({"state": 0, "entry_price": None, "risk_ref": None, "vol_scale": 1.0})
    else:
        action = "HOLD LONG" if prev_pos == 1 else "FLAT"
        msgs.append(f"NAS100: {action} @ {close:.1f} (pivot {pivot:.1f}, resistance {resistance:.1f})")

    state["nas100"] = s
    return state, log


def process_eurgbp(state, msgs):
    log = load_price_log(EURGBP_LOG)
    bar = fetch_latest_daily_bar("EURGBP=X")
    if pd.to_datetime(bar["date"]) in set(log["date"]):
        msgs.append("EURGBP: already logged today.")
        return state, log

    close = bar["close"]
    row = {"date": bar["date"], "close": close, "high": bar["high"], "low": bar["low"]}
    log = pd.concat([log, pd.DataFrame([row])], ignore_index=True)

    ceiling = log["close"].tail(20).max()
    floor = log["close"].tail(20).min()

    s = state["eurgbp"]
    prev_pos = s["state"]

    if close >= ceiling:
        if prev_pos == 1:
            pnl, new_equity = record_trade_close("EURGBP", "Donchian(20)", "long", s["entry_price"], s["risk_ref"], close)
            msgs.append(f"*EURGBP* — REVERSE: exit long @ {close:.5f}. P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
        atr = atr14_from_log(log)
        risk_ref = close + atr if not np.isnan(atr) else close * 1.01
        s.update({"state": -1, "entry_price": close, "risk_ref": risk_ref})
        msgs.append(f"*EURGBP* — ENTER SHORT @ {close:.5f} (risk ref {risk_ref:.5f}, ~£{current_risk_gbp():,.0f} at risk)")
    elif close <= floor:
        if prev_pos == -1:
            pnl, new_equity = record_trade_close("EURGBP", "Donchian(20)", "short", s["entry_price"], s["risk_ref"], close)
            msgs.append(f"*EURGBP* — REVERSE: exit short @ {close:.5f}. P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
        atr = atr14_from_log(log)
        risk_ref = close - atr if not np.isnan(atr) else close * 0.99
        s.update({"state": 1, "entry_price": close, "risk_ref": risk_ref})
        msgs.append(f"*EURGBP* — ENTER LONG @ {close:.5f} (risk ref {risk_ref:.5f}, ~£{current_risk_gbp():,.0f} at risk)")
    else:
        action = {1: "HOLD LONG", -1: "HOLD SHORT", 0: "FLAT"}[prev_pos]
        msgs.append(f"EURGBP: {action} @ {close:.5f} (ceiling {ceiling:.5f}, floor {floor:.5f})")

    state["eurgbp"] = s
    return state, log


def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[warn] Telegram not configured:\n" + msg)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"})


def main():
    state = load_daily_state()
    msgs = []

    state, nas100_log = process_nas100(state, msgs)
    state, eurgbp_log = process_eurgbp(state, msgs)

    nas100_log.to_csv(NAS100_LOG, index=False)
    eurgbp_log.to_csv(EURGBP_LOG, index=False)
    DAILY_STATE_PATH.write_text(json.dumps(state, indent=2))

    equity = load_equity()
    header = f"*Daily Signals — Equity: £{equity:,.0f}*\n\n"
    full_msg = header + "\n".join(msgs)
    send_telegram(full_msg)
    print(full_msg)


if __name__ == "__main__":
    main()
