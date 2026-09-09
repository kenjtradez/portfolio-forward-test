"""
Conditional Volatility Selling + Straddle - SIGNAL ALERTS ONLY.

This does NOT execute trades. Saxo's OpenAPI does not support trading FX
options (a documented, API-wide restriction, confirmed this session) - so
this generates a Telegram alert telling you exactly what to manually place
in SaxoTraderGO, rather than attempting automated execution.

Mechanic (matches the validated backtest exactly):
- Conditional Vol Selling: sell an ATM straddle on a ~21-trading-day cycle,
  ONLY when realized volatility is elevated - specifically, when the
  trailing 20-day realized vol exceeds 1.2x its own trailing 252-day
  average (both computed causally, using .shift(1) so today's decision
  never uses today's own not-yet-complete data).
- Instruments: EURGBP, AUDCAD, EURCAD, EURNZD, XAUUSD - the 5 validated
  in the original backtest.
- Suggested contract: ATM (current price), ~30 calendar days to expiry
  (close to the 21-trading-day cycle length used in the backtest).

Runs on the SAME daily cadence as everything else, sharing state so each
instrument only gets ONE alert per cycle, not a repeated alert every day
the condition holds.
"""
import json
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

BASE = Path(__file__).parent
COND_VOL_LOG = BASE / "cond_vol_log.csv"
COND_VOL_STATE_PATH = BASE / "cond_vol_state.json"

TG_TOKEN = None  # set via environment / GitHub secret, same pattern as daily_signals.py
TG_CHAT_ID = None

COND_VOL_INSTRUMENTS = {
    'EURGBP': 'EURGBP=X', 'AUDCAD': 'AUDCAD=X', 'EURCAD': 'EURCAD=X',
    'EURNZD': 'EURNZD=X', 'XAUUSD': 'GC=F',
}
CYCLE_DAYS = 21
VOL_THRESHOLD = 1.2


def fetch_latest_daily_bar(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "1y", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    timestamps = data["timestamp"]
    quote = data["indicators"]["quote"][0]
    closes = quote["close"]
    valid = [i for i in range(len(closes)) if closes[i] is not None]
    latest_i = valid[-1]
    dt = pd.to_datetime(timestamps[latest_i], unit="s", utc=True).date()
    return {"date": dt, "close": float(closes[latest_i])}


def fetch_full_history(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "2y", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    timestamps = data["timestamp"]
    closes = data["indicators"]["quote"][0]["close"]
    df = pd.DataFrame({"date": pd.to_datetime(timestamps, unit="s", utc=True).date, "close": closes}).dropna()
    return df


def load_log():
    if COND_VOL_LOG.exists():
        return pd.read_csv(COND_VOL_LOG, parse_dates=["date"])
    return pd.DataFrame(columns=["instrument", "date", "close"])


def load_state():
    if COND_VOL_STATE_PATH.exists():
        return json.loads(COND_VOL_STATE_PATH.read_text())
    return {inst: {"last_signal_day_idx": None} for inst in COND_VOL_INSTRUMENTS}


def send_telegram(msg):
    import os
    token = os.environ.get("TG_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        print("[warn] Telegram not configured:\n" + msg)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": chat_id, "text": msg}, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] Telegram send failed: HTTP {r.status_code} - {r.text}")
        else:
            print("[ok] Telegram message sent successfully.")
    except Exception as e:
        print(f"[ERROR] Telegram send raised an exception: {e}")


def main():
    log = load_log()
    state = load_state()
    messages = []

    for inst, yahoo_symbol in COND_VOL_INSTRUMENTS.items():
        bar = fetch_latest_daily_bar(yahoo_symbol)
        inst_log = log[log["instrument"] == inst]

        if len(inst_log) and (inst_log["date"] == pd.Timestamp(bar["date"])).any():
            continue

        row = {"instrument": inst, "date": pd.Timestamp(bar["date"]), "close": bar["close"]}
        log = pd.concat([log, pd.DataFrame([row])], ignore_index=True)
        inst_log = log[log["instrument"] == inst].sort_values("date")

        # seed full 2-year history the first time, so vol calcs have enough lookback immediately
        if len(inst_log) < 260:
            hist = fetch_full_history(yahoo_symbol)
            hist["instrument"] = inst
            hist = hist.rename(columns={"date": "date"})
            hist["date"] = pd.to_datetime(hist["date"])
            log = log[log["instrument"] != inst]
            log = pd.concat([log, hist[["instrument", "date", "close"]]], ignore_index=True)
            inst_log = log[log["instrument"] == inst].sort_values("date")

        closes = inst_log["close"].reset_index(drop=True)
        if len(closes) < 260:
            messages.append(f"{inst}: building history, not enough data yet.")
            continue

        ret = closes.pct_change()
        realized_vol = ret.rolling(20).std() * np.sqrt(252)
        trailing_avg_vol = realized_vol.shift(1).rolling(252).mean()

        cur_vol = realized_vol.iloc[-1]
        cur_avg = trailing_avg_vol.iloc[-1]
        cur_day_idx = len(closes) - 1

        s = state.get(inst, {"last_signal_day_idx": None})
        last_signal = s.get("last_signal_day_idx")
        cycle_elapsed = last_signal is None or (cur_day_idx - last_signal) >= CYCLE_DAYS

        if np.isnan(cur_vol) or np.isnan(cur_avg):
            messages.append(f"{inst}: vol calc not ready yet.")
            continue

        vol_ratio = cur_vol / cur_avg
        current_price = closes.iloc[-1]

        if vol_ratio > VOL_THRESHOLD and cycle_elapsed:
            expiry = (datetime.now().date() + timedelta(days=30)).isoformat()
            msg = (
                f"CONDITIONAL VOL SIGNAL - {inst}\n"
                f"Realized vol is {vol_ratio:.2f}x its trailing 1-year average "
                f"(threshold: {VOL_THRESHOLD}x) - condition MET.\n\n"
                f"Suggested manual action in SaxoTraderGO:\n"
                f"  SELL a straddle (ATM call + ATM put) on {inst}\n"
                f"  Strike (ATM): approx {current_price:.5f}\n"
                f"  Suggested expiry: ~{expiry} (30 days out)\n\n"
                f"This is a signal only - no trade has been placed automatically."
            )
            messages.append(msg)
            s["last_signal_day_idx"] = cur_day_idx
        else:
            reason = "vol not elevated enough" if vol_ratio <= VOL_THRESHOLD else "still within current cycle"
            messages.append(f"{inst}: no signal today ({reason}). Vol ratio: {vol_ratio:.2f}x, price: {current_price:.5f}")

        state[inst] = s

    trimmed = [log[log["instrument"] == inst].sort_values("date").tail(280) for inst in COND_VOL_INSTRUMENTS]
    log = pd.concat(trimmed, ignore_index=True)
    log.to_csv(COND_VOL_LOG, index=False)
    COND_VOL_STATE_PATH.write_text(json.dumps(state, indent=2))

    full_message = "\n\n---\n\n".join(messages)
    send_telegram(full_message)


if __name__ == "__main__":
    main()
