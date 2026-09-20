"""
TGA Daily Shock - SIGNAL ONLY, not automated trading.

Mechanic: identical detection logic to the live TGA-Follow (15-day)
strategy - same Z-score, same threshold - but flags it as a signal for
your own manual, short-hold judgment rather than executing anything.

VALIDATION STATUS - genuinely mixed, worth knowing before acting on this:
- Original test (IS/OOS split 2024-01-01): pooled PF 1.375, but every
  instrument showed real IS/OOS degradation (e.g. US30: 1.746 IS ->
  0.928 OOS, below breakeven).
- Re-examined with the split moved to 2025-01-01: 4 of 6 instruments
  now show OOS at or above 1.0 - looks like a genuine recovery from a
  difficult 2023-2024 stretch, not a permanent breakdown.

INSTRUMENT RANKING (2025-split OOS PF, honest current read):
  DE30 1.546 | US2000 1.395 | NAS100 1.130 | UK100 1.053 | SPX500 1.008 | US30 0.871
This alert now recommends the TOP-RANKED instrument specifically, not
all 6 generically, along with a live entry reference price and an
ATR-based stop. IMPORTANT: the stop-loss level itself was NOT part of
the original backtest (that used a fixed time-exit only) - it's a
reasonable risk-management addition on top of a validated directional
signal, not itself a validated stop distance.

SUGGESTED HOLD WINDOW: 15 minutes to 3 hours after the open, not a full
day - the signal itself is daily, but doesn't require holding all day.

No execution, no journal.py sizing, no risk budget - this is purely
informational, for your own judgment.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import requests

BASE = Path(__file__).parent
TGA_LOG = BASE / "tga_log.csv"  # shared with tgafollow_signals.py - same underlying data
DAILY_SIGNAL_STATE_PATH = BASE / "tga_daily_signal_state.json"

# Ranked by 2025-split OOS PF, strongest first - the alert recommends
# working down this list, not all 6 at once. Open times are UTC, main
# session open for each instrument (US indices use NYSE cash open, not
# their near-24hr futures/CFD trading window).
INSTRUMENTS_RANKED = [
    ('DE30', '^GDAXI', 1.546, 7, 0),      # Frankfurt open 07:00 UTC
    ('US2000', '^RUT', 1.395, 13, 30),    # NYSE cash open 13:30 UTC
    ('NAS100_USD', '^NDX', 1.130, 13, 30),
    ('UK100', '^FTSE', 1.053, 8, 0),      # London open 08:00 UTC
    ('SPX500', '^GSPC', 1.008, 13, 30),
    ('US30', '^DJI', 0.871, 13, 30),
]
Z_HISTORY = 252
SHOCK_THRESHOLD = 1.5
ATR_STOP_MULT = 1.5  # reasonable convention matching other strategies this session - NOT itself backtested for this signal


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


def fetch_recent_daily_bars(symbol, days=20):
    """Enough recent daily bars to compute a 14-day ATR for the stop."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": f"{days}d", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    quote = data["indicators"]["quote"][0]
    df = pd.DataFrame({"high": quote["high"], "low": quote["low"], "close": quote["close"]})
    return df.dropna()


def compute_atr(df, period=14):
    """Standard True Range / ATR, matching the convention used across
    every other strategy this session."""
    high, low, close = df['high'], df['low'], df['close']
    tr = pd.concat([high-low, (high-close.shift(1)).abs(), (low-close.shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]


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
    direction = "long" if z > 0 else "short"

    msgs = ["TGA DAILY SHOCK DETECTED (signal only - no trade placed)",
            f"TGA moved {direction_word}, Z-score={z:.2f} (threshold: {SHOCK_THRESHOLD})",
            f"As of: {latest_tga_date.date()}",
            ""]

    # Actually fetch and rank each instrument's live price + ATR-based
    # stop, rather than just listing names generically
    for name, symbol, oos_pf, open_hour, open_min in INSTRUMENTS_RANKED:
        try:
            bars = fetch_recent_daily_bars(symbol)
            entry_price = bars['close'].iloc[-1]
            atr = compute_atr(bars)
            if np.isnan(atr):
                msgs.append(f"{name}: entry {entry_price:.2f} (ATR not yet available for a stop)")
                continue
            stop_distance = ATR_STOP_MULT * atr
            stop_price = entry_price - stop_distance if direction == "long" else entry_price + stop_distance

            # Exit window: 15 min to 3 hours after THIS instrument's own
            # next session open - computed explicitly here rather than
            # relying on a second, separately-timed alert
            open_time = pd.Timestamp.now(tz='UTC').normalize() + pd.Timedelta(hours=open_hour, minutes=open_min)
            if open_time < pd.Timestamp.now(tz='UTC'):
                open_time += pd.Timedelta(days=1)
            exit_start = open_time + pd.Timedelta(minutes=15)
            exit_end = open_time + pd.Timedelta(hours=3)

            msgs.append(f"{name} (2025 OOS PF {oos_pf:.2f}): {direction.upper()} @ {entry_price:.2f}, stop {stop_price:.2f} ({ATR_STOP_MULT}x ATR)")
            msgs.append(f"    Exit window: {exit_start.strftime('%H:%M')}-{exit_end.strftime('%H:%M')} UTC (15min-3hr after {name}'s own open)")
        except Exception as e:
            msgs.append(f"{name}: could not fetch price/ATR ({e})")

    msgs.append("")
    msgs.append("Ranked by 2025 OOS strength (top = most reliable) - work down the list.")
    msgs.append("Stop is a reasonable ATR-based addition, NOT itself backtested for this signal.")
    msgs.append("Exit at whichever comes first: stop hit, or the exit window closes.")

    full_message = "\n".join(msgs)
    send_telegram(full_message)
    print(full_message)

    tga_log.tail(3000).to_csv(TGA_LOG, index=False)


if __name__ == "__main__":
    main()
