"""
Covered Short Straddle (plain, unconditioned) - SIGNAL ALERTS ONLY.

Same "no automated execution" reasoning as cond_vol_signals.py: Saxo's
OpenAPI does not support trading FX options, so this generates a Telegram
alert telling you exactly what to manually place, rather than attempting
automated execution.

Mechanic (matches the original, unconditioned backtest exactly):
- Sell an ATM straddle on a fixed ~21-trading-day cycle, UNCONDITIONALLY -
  no volatility gate, unlike the separate Conditional Vol Selling signal.
  This is the simpler, original baseline strategy tested before the
  vol-gated improvement was built.
- Instruments: EURGBP, AUDCAD, EURCAD, EURNZD - the 4 instruments this
  specific (non-gated) version was validated on.
- Suggested contract: ATM (current price), ~30 calendar days to expiry.

Runs on the same daily cadence, sharing state so each instrument only
gets ONE alert per ~21-trading-day cycle.
"""
import json
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import requests

BASE = Path(__file__).parent
STRADDLE_LOG = BASE / "straddle_log.csv"
STRADDLE_STATE_PATH = BASE / "straddle_state.json"

STRADDLE_INSTRUMENTS = {
    'EURGBP': 'EURGBP=X', 'AUDCAD': 'AUDCAD=X', 'EURCAD': 'EURCAD=X', 'EURNZD': 'EURNZD=X',
}
CYCLE_DAYS = 21


def fetch_latest_daily_bar(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "5d", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    timestamps = data["timestamp"]
    closes = data["indicators"]["quote"][0]["close"]
    valid = [i for i in range(len(closes)) if closes[i] is not None]
    latest_i = valid[-1]
    dt = pd.to_datetime(timestamps[latest_i], unit="s", utc=True).date()
    return {"date": dt, "close": float(closes[latest_i])}


def load_log():
    if STRADDLE_LOG.exists():
        return pd.read_csv(STRADDLE_LOG, parse_dates=["date"])
    return pd.DataFrame(columns=["instrument", "date", "close"])


def load_state():
    if STRADDLE_STATE_PATH.exists():
        return json.loads(STRADDLE_STATE_PATH.read_text())
    return {inst: {"last_signal_day_idx": None} for inst in STRADDLE_INSTRUMENTS}


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

    for inst, yahoo_symbol in STRADDLE_INSTRUMENTS.items():
        bar = fetch_latest_daily_bar(yahoo_symbol)
        inst_log = log[log["instrument"] == inst]

        if len(inst_log) and (inst_log["date"] == pd.Timestamp(bar["date"])).any():
            continue

        row = {"instrument": inst, "date": pd.Timestamp(bar["date"]), "close": bar["close"]}
        log = pd.concat([log, pd.DataFrame([row])], ignore_index=True)
        inst_log = log[log["instrument"] == inst].sort_values("date")
        cur_day_idx = len(inst_log) - 1
        current_price = bar["close"]

        s = state.get(inst, {"last_signal_day_idx": None})
        last_signal = s.get("last_signal_day_idx")
        cycle_elapsed = last_signal is None or (cur_day_idx - last_signal) >= CYCLE_DAYS

        if cycle_elapsed:
            expiry = (datetime.now().date() + timedelta(days=30)).isoformat()
            msg = (
                f"STRADDLE SIGNAL - {inst}\n"
                f"Unconditioned ~21-day cycle - time to sell.\n\n"
                f"Suggested manual action in SaxoTraderGO:\n"
                f"  SELL a straddle (ATM call + ATM put) on {inst}\n"
                f"  Strike (ATM): approx {current_price:.5f}\n"
                f"  Suggested expiry: ~{expiry} (30 days out)\n\n"
                f"This is a signal only - no trade has been placed automatically."
            )
            messages.append(msg)
            s["last_signal_day_idx"] = cur_day_idx
        else:
            days_left = CYCLE_DAYS - (cur_day_idx - last_signal)
            messages.append(f"{inst}: no signal today ({days_left} trading days left in current cycle). Price: {current_price:.5f}")

        state[inst] = s

    trimmed = [log[log["instrument"] == inst].sort_values("date").tail(50) for inst in STRADDLE_INSTRUMENTS]
    log = pd.concat(trimmed, ignore_index=True)
    log.to_csv(STRADDLE_LOG, index=False)
    STRADDLE_STATE_PATH.write_text(json.dumps(state, indent=2))

    full_message = "\n\n---\n\n".join(messages)
    send_telegram(full_message)


if __name__ == "__main__":
    main()
