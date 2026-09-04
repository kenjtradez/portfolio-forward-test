"""
Hourly forward-test signal logger — ADX(14)>25 + Supertrend(10,3), 12 pairs,
with 1% risk-based journaling. See journal.py for methodology.

This system already has a genuine trailing stop (the Supertrend line), so
its risk reference is the REAL stop distance at entry — no ATR proxy
needed here, unlike the two daily strategies.

Requires env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Data source: Yahoo Finance
"""
import os
import json
import requests
import pandas as pd
import numpy as np
from pathlib import Path
from journal import record_trade_close, current_risk_gbp, load_equity, available_risk_fraction
from execution import execute_entry, execute_exit, update_trailing_stop, EXECUTION_ENABLED

BASE = Path(__file__).parent
PRICE_LOG_PATH = BASE / "hourly_price_log.csv"
HOURLY_STATE_PATH = BASE / "hourly_state.json"

ADX_THRESH = 25
ST_PERIOD = 10
ST_MULT = 3.0
BREAKEVEN_AT_R = 1.0
MIN_HISTORY_BARS = 60

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

YAHOO_SYMBOLS = {
    'AUDCAD': 'AUDCAD=X', 'AUDCHF': 'AUDCHF=X', 'AUDJPY': 'AUDJPY=X', 'AUDNZD': 'AUDNZD=X',
    'AUDUSD': 'AUDUSD=X', 'CADCHF': 'CADCHF=X', 'CADJPY': 'CADJPY=X', 'CHFJPY': 'CHFJPY=X',
    'EURAUD': 'EURAUD=X', 'EURCAD': 'EURCAD=X', 'EURCHF': 'EURCHF=X', 'EURGBP': 'EURGBP=X',
    'EURJPY': 'EURJPY=X', 'EURNZD': 'EURNZD=X', 'EURUSD': 'EURUSD=X', 'GBPAUD': 'GBPAUD=X',
    'GBPCAD': 'GBPCAD=X', 'GBPCHF': 'GBPCHF=X', 'GBPJPY': 'GBPJPY=X', 'GBPNZD': 'GBPNZD=X',
    'GBPUSD': 'GBPUSD=X', 'NZDCAD': 'NZDCAD=X', 'NZDJPY': 'NZDJPY=X', 'NZDUSD': 'NZDUSD=X',
    'USDCAD': 'USDCAD=X', 'USDCHF': 'USDCHF=X', 'USDJPY': 'USDJPY=X',
    'NAS100_USD': '^NDX', 'SPX500': '^GSPC', 'US30': '^DJI', 'US2000': '^RUT',
    'DE30': '^GDAXI', 'UK100': '^FTSE', 'XAUUSD': 'XAUUSD=X',
}
PAIRS = list(YAHOO_SYMBOLS.keys())


def fetch_latest_h1_bars(symbol, n=5):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "5d", "interval": "60m"}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"Yahoo error for {symbol}: {data}")
    result = result[0]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    bars = []
    for i in range(len(timestamps)):
        if quote["close"][i] is None:
            continue
        bars.append({
            "datetime": pd.to_datetime(timestamps[i], unit="s", utc=True),
            "open": quote["open"][i], "high": quote["high"][i],
            "low": quote["low"][i], "close": quote["close"][i],
        })
    return bars[-n:]


def wilder_smooth(series, n):
    result = np.full(len(series), np.nan)
    vals = series.values
    if len(vals) <= n:
        return pd.Series(result, index=series.index)
    result[n] = np.nansum(vals[1:n + 1])
    for i in range(n + 1, len(vals)):
        result[i] = result[i - 1] - (result[i - 1] / n) + vals[i]
    return pd.Series(result, index=series.index)


def compute_adx(df, n=14):
    high, low, close = df['high'], df['low'], df['close']
    up_move, down_move = high.diff(), -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr_w = wilder_smooth(tr, n)
    plus_di = 100 * (wilder_smooth(plus_dm, n) / atr_w)
    minus_di = 100 * (wilder_smooth(minus_dm, n) / atr_w)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.rolling(n).mean()


def compute_supertrend(df, period=10, mult=3.0):
    high, low, close = df['high'].values, df['low'].values, df['close'].values
    tr = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift(1)).abs(),
                     (df['low'] - df['close'].shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().values
    hl2 = (high + low) / 2
    basic_upper, basic_lower = hl2 + mult * atr, hl2 - mult * atr
    n = len(df)
    final_upper, final_lower = np.full(n, np.nan), np.full(n, np.nan)
    supertrend, direction = np.full(n, np.nan), np.zeros(n)
    for i in range(n):
        if np.isnan(atr[i]):
            continue
        if i == 0 or np.isnan(final_upper[i - 1]):
            final_upper[i], final_lower[i] = basic_upper[i], basic_lower[i]
            direction[i] = 1
            supertrend[i] = final_lower[i]
            continue
        final_upper[i] = basic_upper[i] if (basic_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]) else final_upper[i - 1]
        final_lower[i] = basic_lower[i] if (basic_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]) else final_lower[i - 1]
        if direction[i - 1] == 1:
            direction[i] = -1 if close[i] < final_lower[i] else 1
        else:
            direction[i] = 1 if close[i] > final_upper[i] else -1
        supertrend[i] = final_lower[i] if direction[i] == 1 else final_upper[i]
    return pd.Series(supertrend, index=df.index), pd.Series(direction, index=df.index)


def load_price_log():
    if PRICE_LOG_PATH.exists():
        return pd.read_csv(PRICE_LOG_PATH, parse_dates=["datetime"])
    raise FileNotFoundError(f"{PRICE_LOG_PATH} not found — seed it before first run.")


def load_state():
    if HOURLY_STATE_PATH.exists():
        return json.loads(HOURLY_STATE_PATH.read_text())
    return {p: {"state": 0, "entry_price": None, "initial_risk_ref": None,
                "stop_price": None, "moved_to_be": False, "trade_id": None} for p in PAIRS}


def process_pair(pair, price_log, state, open_counter):
    symbol = YAHOO_SYMBOLS[pair]
    bars = fetch_latest_h1_bars(symbol, n=5)
    if not bars:
        return None, price_log, "no new data"

    pair_log = price_log[price_log["pair"] == pair].sort_values("datetime")
    existing_dts = set(pair_log["datetime"])
    new_rows = [b for b in bars if b["datetime"] not in existing_dts]
    if not new_rows:
        return None, price_log, "no new bar"

    for b in new_rows:
        price_log = pd.concat([price_log, pd.DataFrame([{**b, "pair": pair}])], ignore_index=True)

    pair_df = price_log[price_log["pair"] == pair].sort_values("datetime").set_index("datetime")
    if len(pair_df) < MIN_HISTORY_BARS:
        return None, price_log, f"insufficient history ({len(pair_df)} bars)"

    adx = compute_adx(pair_df, 14)
    st, st_dir = compute_supertrend(pair_df, ST_PERIOD, ST_MULT)
    closes, highs, lows = pair_df['close'].values, pair_df['high'].values, pair_df['low'].values
    st_vals, dir_vals, adx_vals = st.values, st_dir.values, adx.values

    p_state = state.get(pair, {"state": 0, "entry_price": None, "initial_risk_ref": None,
                                "stop_price": None, "moved_to_be": False, "trade_id": None, "risk_fraction": 1.0})
    cur_pos = p_state["state"]
    entry_price = p_state["entry_price"]
    initial_risk_ref = p_state["initial_risk_ref"]
    stop_price = p_state["stop_price"]
    moved_to_be = p_state["moved_to_be"]
    trade_id = p_state.get("trade_id")
    risk_fraction = p_state.get("risk_fraction", 1.0)

    i = len(pair_df) - 1
    c, h, l = closes[i], highs[i], lows[i]
    prev_dir, cur_dir = dir_vals[i - 1], dir_vals[i]
    cur_st, cur_adx = st_vals[i], adx_vals[i]

    action = "HOLD" if cur_pos != 0 else "FLAT"
    pnl_note = None
    stop_updated = False

    if cur_pos == 0:
        if not np.isnan(cur_adx) and cur_adx > ADX_THRESH and not np.isnan(cur_st):
            if prev_dir == -1 and cur_dir == 1:
                risk_frac = available_risk_fraction(open_counter[0])
                if risk_frac <= 0:
                    action = "SIGNAL SKIPPED (risk budget full)"
                else:
                    cur_pos = 1
                    entry_price = c
                    stop_price = st_vals[i - 1]
                    initial_risk_ref = stop_price
                    moved_to_be = False
                    risk_fraction = risk_frac
                    action = "ENTER LONG" if risk_frac >= 1.0 else f"ENTER LONG ({risk_frac:.0%} slice)"
                    fill = execute_entry(pair, "long", current_risk_gbp(risk_frac), stop_price)
                    trade_id = fill["trade_id"] if fill else None
                    open_counter[0] += 1
            elif prev_dir == 1 and cur_dir == -1:
                risk_frac = available_risk_fraction(open_counter[0])
                if risk_frac <= 0:
                    action = "SIGNAL SKIPPED (risk budget full)"
                else:
                    cur_pos = -1
                    entry_price = c
                    stop_price = st_vals[i - 1]
                    initial_risk_ref = stop_price
                    moved_to_be = False
                    risk_fraction = risk_frac
                    action = "ENTER SHORT" if risk_frac >= 1.0 else f"ENTER SHORT ({risk_frac:.0%} slice)"
                    fill = execute_entry(pair, "short", current_risk_gbp(risk_frac), stop_price)
                    trade_id = fill["trade_id"] if fill else None
                    open_counter[0] += 1
    elif cur_pos == 1:
        if not np.isnan(cur_st) and cur_st > stop_price:
            stop_price = cur_st
            stop_updated = True
        if not moved_to_be and initial_risk_ref and (c - entry_price) >= BREAKEVEN_AT_R * (entry_price - initial_risk_ref):
            stop_price = max(stop_price, entry_price)
            moved_to_be = True
            stop_updated = True
            action = "HOLD LONG (-> breakeven)"
        if l <= stop_price:
            # broker's own attached stop handles this — don't call execute_exit,
            # the position is already closed at OANDA if execution is live
            pnl, new_equity = record_trade_close(pair, "ADX+Supertrend", "long", entry_price, initial_risk_ref, stop_price, risk_fraction)
            pnl_note = (pnl, new_equity)
            action = "EXIT LONG (stop hit)"
            cur_pos, entry_price, stop_price, initial_risk_ref, moved_to_be, trade_id, risk_fraction = 0, None, None, None, False, None, 1.0
        elif cur_dir == -1:
            # reversal signal fires before the stop was touched — this needs
            # an explicit close, the broker's stop wouldn't have fired yet
            if trade_id:
                execute_exit(trade_id)
            pnl, new_equity = record_trade_close(pair, "ADX+Supertrend", "long", entry_price, initial_risk_ref, c, risk_fraction)
            pnl_note = (pnl, new_equity)
            action = "EXIT LONG (reversal)"
            cur_pos, entry_price, stop_price, initial_risk_ref, moved_to_be, trade_id, risk_fraction = 0, None, None, None, False, None, 1.0
        elif stop_updated and trade_id:
            update_trailing_stop(trade_id, stop_price)
            if action == "HOLD":
                action = "HOLD LONG"
        elif action == "HOLD":
            action = "HOLD LONG"
    elif cur_pos == -1:
        if not np.isnan(cur_st) and cur_st < stop_price:
            stop_price = cur_st
            stop_updated = True
        if not moved_to_be and initial_risk_ref and (entry_price - c) >= BREAKEVEN_AT_R * (initial_risk_ref - entry_price):
            stop_price = min(stop_price, entry_price)
            moved_to_be = True
            stop_updated = True
            action = "HOLD SHORT (-> breakeven)"
        if h >= stop_price:
            pnl, new_equity = record_trade_close(pair, "ADX+Supertrend", "short", entry_price, initial_risk_ref, stop_price, risk_fraction)
            pnl_note = (pnl, new_equity)
            action = "EXIT SHORT (stop hit)"
            cur_pos, entry_price, stop_price, initial_risk_ref, moved_to_be, trade_id, risk_fraction = 0, None, None, None, False, None, 1.0
        elif cur_dir == 1:
            if trade_id:
                execute_exit(trade_id)
            pnl, new_equity = record_trade_close(pair, "ADX+Supertrend", "short", entry_price, initial_risk_ref, c, risk_fraction)
            pnl_note = (pnl, new_equity)
            action = "EXIT SHORT (reversal)"
            cur_pos, entry_price, stop_price, initial_risk_ref, moved_to_be, trade_id, risk_fraction = 0, None, None, None, False, None, 1.0
        elif stop_updated and trade_id:
            update_trailing_stop(trade_id, stop_price)
            if action == "HOLD":
                action = "HOLD SHORT"
        elif action == "HOLD":
            action = "HOLD SHORT"

    state[pair] = {"state": cur_pos, "entry_price": entry_price, "initial_risk_ref": initial_risk_ref,
                    "stop_price": stop_price, "moved_to_be": moved_to_be, "trade_id": trade_id, "risk_fraction": risk_fraction}

    detail = {"pair": pair, "close": c, "action": action, "position": cur_pos, "pnl_note": pnl_note}
    return detail, price_log, "ok"


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

    price_log = load_price_log()
    state = load_state()
    details = []
    open_counter = [0]  # tracks new entries opened earlier in this same run, see available_risk_fraction()

    for pair in PAIRS:
        try:
            detail, price_log, status = process_pair(pair, price_log, state, open_counter)
            details.append(detail if detail else {"pair": pair, "action": f"SKIPPED ({status})"})
        except Exception as e:
            details.append({"pair": pair, "action": f"ERROR: {e}"})

    trimmed = [price_log[price_log["pair"] == p].sort_values("datetime").tail(1500) for p in PAIRS]
    price_log = pd.concat(trimmed, ignore_index=True)
    price_log.to_csv(PRICE_LOG_PATH, index=False)
    HOURLY_STATE_PATH.write_text(json.dumps(state, indent=2))

    active = [d for d in details if d.get("action", "").startswith(("ENTER", "EXIT")) or "breakeven" in d.get("action", "")]
    env_tag = "[LIVE TRADING]" if EXECUTION_ENABLED else "[forward-test / signal only]"
    if active:
        equity = load_equity()
        lines = [f"*ADX+Supertrend Update {env_tag} — Equity: £{equity:,.0f}*\n"]
        for d in active:
            line = f"{d['pair']}: *{d['action']}* @ {d.get('close', '?')}"
            if d.get("pnl_note"):
                pnl, eq = d["pnl_note"]
                line += f" — P&L £{pnl:,.0f}"
            else:
                line += f" — ~£{current_risk_gbp():,.0f} at risk"
            lines.append(line)
        send_telegram("\n".join(lines))
    else:
        print("No position changes this run.")

    for d in details:
        print(d)


if __name__ == "__main__":
    main()
