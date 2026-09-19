"""
TGA Daily Shock - SIGNAL ONLY, not automated trading.

Matches the "threshold" style workflow from your existing tracked system:
  Threshold 1 (TGA shock detected) -> Threshold 2 (would confirm, but
  this signal is fast enough that confirmation isn't part of its own
  validated design) -> a Telegram alert, not an automated order.

Mechanic: identical detection logic to the live TGA-Follow (15-day)
strategy - same Z-score, same threshold - but flags it as a 1-day-hold
opportunity instead of executing anything. This exists SEPARATELY from
the live strategy because the 1-day version, while showing a higher
backtested Sharpe, showed real IS/OOS degradation on every single
instrument tested (unlike the 15-day version, which was consistent to
improving) - not reliable enough to trust with automated capital, but
real enough to be worth your own eyes on, given the significant
historical edge it did show in-sample.

No execution, no journal.py sizing, no risk budget - this is purely
informational, the same way the original Divergence-Fade and COT alerts
started before their own graduation to live strategies (if they ever
warrant it, unlike this one which has an active reliability concern).
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import requests

BASE = Path(__file__).parent
TGA_LOG = BASE / "tga_log.csv"  # shared with tgafollow_signals.py - same underlying data
DAILY_SIGNAL_STATE_PATH = BASE / "tga_daily_signal_state.json"

INSTRUMENTS = {
    'NAS100_USD': '^NDX', 'SPX500': '^GSPC', 'US30': '^DJI',
    'US2000': '^RUT', 'DE30': '^GDAXI', 'UK100': '^FTSE',
}
Z_HISTORY = 252
SHOCK_THRESHOLD = 1.5


def fetch_latest_tga():
    url = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance"
    params = {"sort": "-record_date", "page[size]": "20"}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    df = pd.DataFrame(r.json()["data"])
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
    closes = data["indicators"]["quote"][0]["close"]
    valid = [i for i in range(len(closes)) if closes[i] is not None]
    latest_i = valid[-1]
    dt = pd.to_datetime(data["timestamp"][latest_i], unit="s", utc=True).date()
    return {"date": dt, "close": float(closes[latest_i])}


def load_tga_log():
    if TGA_LOG.exists():
        return pd.read_csv(TGA_LOG, parse_dates=["date"])
    return pd.DataFrame(columns=["date", "balance"])


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


def main():
    tga_log = load_tga_log()
    tga_new = fetch_latest_tga()
    tga_log = pd.concat([tga_log, tga_new], ignore_index=True).drop_duplicates(subset='date', keep='last').sort_values('date')
    tga_series = tga_log.set_index('date')['balance']
    tga_chg = tga_series.diff(1)

    if len(tga_chg.dropna()) < Z_HISTORY + 10:
        print("Building TGA history, not enough data yet.")
        return

    std_252 = tga_chg.rolling(Z_HISTORY).std()
    z = tga_chg.iloc[-1] / std_252.iloc[-1] if std_252.iloc[-1] > 0 else np.nan
    latest_tga_date = tga_series.index[-1]

    msgs = []
    if np.isnan(z) or abs(z) < SHOCK_THRESHOLD:
        print(f"No TGA shock today (Z={z:.2f}). No alert sent.")
        return

    direction_word = "UP" if z > 0 else "DOWN"
    msgs.append("TGA DAILY SHOCK DETECTED (signal only - no trade placed)")
    msgs.append(f"TGA moved {direction_word}, Z-score={z:.2f} (threshold: {SHOCK_THRESHOLD})")
    msgs.append(f"As of: {latest_tga_date.date()}")
    msgs.append("")
    msgs.append("Historical backtest (1-day hold, FOLLOW direction) showed real edge in-sample,")
    msgs.append("but degraded out-of-sample on every instrument tested - NOT used for automated")
    msgs.append("trading. This is for your own judgment only, on:")
    msgs.append("NAS100, SPX500, US30, US2000, DE30, UK100")

    full_message = "\n".join(msgs)
    send_telegram(full_message)
    print(full_message)

    tga_log.tail(3000).to_csv(TGA_LOG, index=False)


if __name__ == "__main__":
    main()
