"""
TGA-Follow Liquidity Strategy - NAS100, SPX500, US30, US2000, DE30, UK100.

Mechanic (matches the validated backtest exactly):
- Each day, check yesterday's change in the Treasury General Account
  (TGA) balance, published daily by the US Treasury (~4pm ET for the
  prior business day).
- When that change's magnitude exceeds 1.5 standard deviations of its
  own trailing 252-day history: FOLLOW it, not fade it - a genuine,
  real momentum effect (unusual among everything validated this
  session, which has otherwise all been mean-reversion). Enter in the
  SAME direction the TGA moved. Hold 15 trading days, then exit
  regardless of price.

Validated results (6 equity indices pooled): PF 1.420, 100th-percentile
randomization, IS/OOS consistent-to-improving on every single instrument
(the key reason 15-day was chosen over the superficially higher-Sharpe
1-day version, which showed real IS/OOS degradation on every instrument
tested). Robust to 5x cost stress (PF 1.324).

RISK SIZING: this account has a hard 6% max-drawdown limit (funded
account). Sized conservatively pending real forward-test history,
matching the same approach as every other new strategy this session -
see journal.py's STRATEGY_RISK_PCT for the exact value and derivation.
"""
import json
from pathlib import Path
from io import StringIO

import numpy as np
import pandas as pd
import requests

from journal import (
    record_trade_close, available_risk_fraction, current_risk_gbp, get_risk_pct,
)
from execution import execute_entry, execute_exit, EXECUTION_ENABLED

BASE = Path(__file__).parent
TGA_LOG = BASE / "tga_log.csv"
PRICE_LOG_DIR = BASE  # per-instrument price logs: tgafollow_price_{inst}.csv
STATE_PATH = BASE / "tgafollow_full_state.json"

INSTRUMENTS = {
    'NAS100_USD': '^NDX', 'SPX500': '^GSPC', 'US30': '^DJI',
    'US2000': '^RUT', 'DE30': '^GDAXI', 'UK100': '^FTSE',
}
Z_HISTORY = 252
SHOCK_THRESHOLD = 1.5
HOLD_DAYS = 15


def fetch_latest_tga():
    """US Treasury's Daily Treasury Statement, Operating Cash Balance
    table - published daily, no API key needed. Handles both the pre-
    and post-April-2022 formats (confirmed necessary during this
    strategy's own historical data build)."""
    url = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance"
    params = {"sort": "-record_date", "page[size]": "20"}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()["data"]
    df = pd.DataFrame(data)

    era1 = df[df['account_type'].isin(['Federal Reserve Account', 'Treasury General Account (TGA)'])][['record_date', 'close_today_bal']].copy()
    era1.columns = ['date', 'balance']
    era2 = df[df['account_type'] == 'Treasury General Account (TGA) Closing Balance'][['record_date', 'open_today_bal']].copy()
    era2.columns = ['date', 'balance']

    combined = pd.concat([era1, era2], ignore_index=True)
    combined['date'] = pd.to_datetime(combined['date'])
    combined['balance'] = pd.to_numeric(combined['balance'], errors='coerce')
    return combined.dropna().drop_duplicates(subset='date').sort_values('date')


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


def load_tga_log():
    if TGA_LOG.exists():
        return pd.read_csv(TGA_LOG, parse_dates=["date"])
    return pd.DataFrame(columns=["date", "balance"])


def load_price_log(inst):
    path = PRICE_LOG_DIR / f"tgafollow_price_{inst}.csv"
    if path.exists():
        return pd.read_csv(path, parse_dates=["date"])
    return pd.DataFrame(columns=["date", "close"])


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {inst: {"state": 0, "entry_price": None, "entry_date": None,
                    "trade_id": None, "risk_fraction": 1.0} for inst in INSTRUMENTS}


def send_telegram(msg):
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
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


def process_tgafollow_all(state, msgs, open_counter):
    tga_log = load_tga_log()
    tga_new = fetch_latest_tga()
    tga_log = pd.concat([tga_log, tga_new], ignore_index=True).drop_duplicates(subset='date', keep='last').sort_values('date')

    tga_series = tga_log.set_index('date')['balance']
    tga_chg = tga_series.diff(1)

    tgafollow_state = state.get("tgafollow", load_state())

    for inst, yahoo_symbol in INSTRUMENTS.items():
        bar = fetch_latest_daily_bar(yahoo_symbol)
        price_log = load_price_log(inst)
        if len(price_log) and (price_log["date"] == pd.Timestamp(bar["date"])).any():
            continue
        price_log = pd.concat([price_log, pd.DataFrame([{"date": pd.Timestamp(bar["date"]), "close": bar["close"]}])], ignore_index=True)
        price_log = price_log.tail(400)
        price_log.to_csv(PRICE_LOG_DIR / f"tgafollow_price_{inst}.csv", index=False)

        s = tgafollow_state.get(inst, {"state": 0, "entry_price": None, "entry_date": None,
                                        "trade_id": None, "risk_fraction": 1.0})
        close = bar["close"]
        cur_date = pd.Timestamp(bar["date"])

        if s["state"] != 0:
            entry_date = pd.Timestamp(s["entry_date"])
            days_held = (price_log['date'] > entry_date).sum()
            if days_held >= HOLD_DAYS:
                direction = "long" if s["state"] == 1 else "short"
                if s.get("trade_id"):
                    execute_exit(s["trade_id"])
                pnl, new_equity = record_trade_close(inst, "TGA-Follow", direction, s["entry_price"], s["entry_price"]*0.99, close, s.get("risk_fraction", 1.0))
                msgs.append(f"*{inst}* — EXIT {direction.upper()} @ {close:.2f} (TGA-Follow, {HOLD_DAYS}-day hold complete). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
                s.update({"state": 0, "entry_price": None, "entry_date": None, "trade_id": None, "risk_fraction": 1.0})
            else:
                action = "HOLD LONG" if s["state"] == 1 else "HOLD SHORT"
                msgs.append(f"{inst} (TGA-Follow): {action} @ {close:.2f} (day {days_held+1}/{HOLD_DAYS})")
            tgafollow_state[inst] = s
            continue

        if len(tga_chg.dropna()) < Z_HISTORY + 10:
            msgs.append(f"{inst} (TGA-Follow): building TGA history.")
            tgafollow_state[inst] = s
            continue

        std_252 = tga_chg.rolling(Z_HISTORY).std()
        z = tga_chg.iloc[-1] / std_252.iloc[-1] if std_252.iloc[-1] > 0 else np.nan
        latest_tga_date = tga_series.index[-1]
        if (cur_date - latest_tga_date).days > 3:
            msgs.append(f"{inst} (TGA-Follow): TGA data stale, skipping.")
            tgafollow_state[inst] = s
            continue

        if np.isnan(z) or abs(z) < SHOCK_THRESHOLD:
            msgs.append(f"{inst} (TGA-Follow, Z={z:.2f}): FLAT @ {close:.2f}")
            tgafollow_state[inst] = s
            continue

        direction = "long" if z > 0 else "short"  # FOLLOW the TGA move's direction
        risk_frac = available_risk_fraction("TGA-Follow", open_counter[0])
        if risk_frac <= 0:
            msgs.append(f"{inst}: TGA shock signal fired (Z={z:.2f}) but SKIPPED — 10% risk budget full.")
        else:
            risk_ref = close * (0.99 if direction == "long" else 1.01)
            risk_gbp = current_risk_gbp("TGA-Follow", risk_frac)
            fill = execute_entry(inst, direction, risk_gbp, risk_ref)
            trade_id = fill["trade_id"] if fill else None
            s.update({"state": 1 if direction == "long" else -1, "entry_price": close,
                      "entry_date": cur_date.isoformat(), "trade_id": trade_id, "risk_fraction": risk_frac})
            open_counter[0] += get_risk_pct("TGA-Follow")
            exec_note = f" [LIVE, trade {trade_id}]" if fill else (" [EXECUTION ENABLED but order failed]" if EXECUTION_ENABLED else "")
            msgs.append(f"*{inst}* (TGA-Follow, Z={z:.2f}) — ENTER {direction.upper()} @ {close:.2f} (~£{risk_gbp:,.0f} at risk){exec_note}")

        tgafollow_state[inst] = s

    state["tgafollow"] = tgafollow_state
    return state, tga_log


def main():
    full_state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    msgs = []
    open_counter = [0]

    full_state, tga_log = process_tgafollow_all(full_state, msgs, open_counter)

    tga_log.tail(3000).to_csv(TGA_LOG, index=False)
    STATE_PATH.write_text(json.dumps(full_state, indent=2))

    full_message = "\n".join(msgs)
    send_telegram(full_message)
    print(full_message)


if __name__ == "__main__":
    main()
