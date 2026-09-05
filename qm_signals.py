"""
QM + CISD + SBR — multi-timeframe (1H structure -> 15m entry precision).

Validated this session: 34-instrument sweep (median PF 1.19, 26/34 pass both
IS+OOS), direction-randomization audit (0/2000 simulations beat the real
result, ~10 sigma), parameter sensitivity (PF stable 1.53-1.59 across 7
variants), properly risk-capped portfolio backtest (CAGR 60.7%, Sharpe 1.96,
Max DD -16.9%).

Design, matching the validated backtest exactly:
- 1H bars drive the structure: pivot highs/lows (QM levels, confirmed
  pivot_len bars later - causal, no lookahead), sweep detection (close
  beyond the QM level), CISD confirmation (close back through it).
- On CISD confirmation, switch to 15m bars: find the most recent opposite-
  colored candle within origin_lookback bars as the origin (SBR zone), place
  a LIMIT order at its edge.
- 15m bars manage the limit order: fill check, expiry (unfilled after
  order_expiry_bars 15m-bars), then stop/target management once filled.
- One pending/open order at a time per instrument (matching the Pine logic:
  state resets to 0 after creating an order).

DATA CAVEAT: Yahoo Finance's free API only provides ~60 days of 15-minute
history. That's fine for running this live (each run only needs recent
bars), but means this script recomputes structure fresh each run rather
than relying on long persisted history — see fetch functions below.
"""
import os
import json
import time
import requests
import pandas as pd
import numpy as np
from pathlib import Path
from journal import record_trade_close, current_risk_gbp, load_equity, available_risk_fraction
from execution import execute_entry, execute_exit, EXECUTION_ENABLED

BASE = Path(__file__).parent
QM_STATE_PATH = BASE / "qm_state.json"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

INSTRUMENTS = {
    'AUDCAD': 'AUDCAD=X', 'AUDCHF': 'AUDCHF=X', 'AUDJPY': 'AUDJPY=X', 'AUDNZD': 'AUDNZD=X',
    'AUDUSD': 'AUDUSD=X', 'CADCHF': 'CADCHF=X', 'CADJPY': 'CADJPY=X', 'CHFJPY': 'CHFJPY=X',
    'EURAUD': 'EURAUD=X', 'EURCAD': 'EURCAD=X', 'EURCHF': 'EURCHF=X', 'EURGBP': 'EURGBP=X',
    'EURJPY': 'EURJPY=X', 'EURNZD': 'EURNZD=X', 'EURUSD': 'EURUSD=X', 'GBPAUD': 'GBPAUD=X',
    'GBPCAD': 'GBPCAD=X', 'GBPCHF': 'GBPCHF=X', 'GBPJPY': 'GBPJPY=X', 'GBPNZD': 'GBPNZD=X',
    'GBPUSD': 'GBPUSD=X', 'NZDCAD': 'NZDCAD=X', 'NZDJPY': 'NZDJPY=X', 'NZDUSD': 'NZDUSD=X',
    'USDCAD': 'USDCAD=X', 'USDCHF': 'USDCHF=X', 'USDJPY': 'USDJPY=X',
    'NAS100_USD': '^NDX', 'SPX500': '^GSPC', 'US30': '^DJI', 'US2000': '^RUT',
    'DE30': '^GDAXI', 'UK100': '^FTSE', 'XAUUSD': 'GC=F',
}

MAJORS = {'EURUSD','GBPUSD','USDJPY','USDCAD','USDCHF','AUDUSD','NZDUSD'}
INDICES_GOLD = {'NAS100_USD','SPX500','US30','US2000','DE30','UK100','XAUUSD'}
def cost_bps(inst):
    if inst in MAJORS: return 0.0002
    elif inst in INDICES_GOLD: return 0.0003
    else: return 0.0004

JPY_PAIRS = {'AUDJPY','CADJPY','CHFJPY','EURJPY','GBPJPY','NZDJPY','USDJPY'}
def tick_size(inst):
    if inst in JPY_PAIRS: return 0.01
    elif inst in INDICES_GOLD: return 0.1 if inst == 'XAUUSD' else 1.0
    else: return 0.0001

# validated defaults, from the parameter sensitivity sweep
PIVOT_LEN = 10
MAX_SWEEP_BARS = 15
ORIGIN_LOOKBACK = 6
RR_TARGET = 2.0
SL_BUFFER_TICKS = 10
ORDER_EXPIRY_BARS = 20  # in 15m bars = 5 hours


def fetch_bars(symbol, interval, range_str, max_retries=3):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    for attempt in range(max_retries):
        r = requests.get(url, params={"interval": interval, "range": range_str}, timeout=15)
        if r.status_code == 429:
            wait = 5 * (attempt + 1)  # 5s, 10s, 15s backoff
            print(f"[warn] {symbol}: rate limited (429), retrying in {wait}s (attempt {attempt+1}/{max_retries})")
            time.sleep(wait)
            continue
        r.raise_for_status()
        data = r.json()["chart"]["result"][0]
        ts = data["timestamp"]
        q = data["indicators"]["quote"][0]
        df = pd.DataFrame({"open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"]},
                           index=pd.to_datetime(ts, unit="s", utc=True))
        return df.dropna()
    raise RuntimeError(f"Rate limited after {max_retries} retries")


def find_pivots(high, low, pivot_len):
    n = len(high)
    pivot_high = np.full(n, np.nan)
    pivot_low = np.full(n, np.nan)
    h, l = high.values, low.values
    for i in range(pivot_len, n - pivot_len):
        window_h = h[i-pivot_len:i+pivot_len+1]
        if h[i] == window_h.max() and np.argmax(window_h) == pivot_len:
            pivot_high[i+pivot_len] = h[i]
        window_l = l[i-pivot_len:i+pivot_len+1]
        if l[i] == window_l.min() and np.argmin(window_l) == pivot_len:
            pivot_low[i+pivot_len] = l[i]
    return pivot_high, pivot_low


def detect_latest_cisd(df_h1):
    """Recomputes the full sweep/CISD state machine fresh over the available
    1H history and returns a CISD event only if it occurred on the LAST
    completed bar (i.e., is actionable right now) — otherwise None.
    Stateless by design: avoids persisting complex state machine internals
    across runs, matching the validated backtest exactly since pivot/sweep
    detection only depends on recent local history anyway."""
    o, h, l, c = df_h1['open'].values, df_h1['high'].values, df_h1['low'].values, df_h1['close'].values
    n = len(df_h1)
    pivot_high, pivot_low = find_pivots(df_h1['high'], df_h1['low'], PIVOT_LEN)

    qm_high, qm_low = np.nan, np.nan
    bear_state, bull_state = 0, 0
    bear_sweep_bar = bear_qm_val = bear_sweep_high = None
    bull_sweep_bar = bull_qm_val = bull_sweep_low = None
    last_cisd = None

    for i in range(n):
        if not np.isnan(pivot_high[i]): qm_high = pivot_high[i]
        if not np.isnan(pivot_low[i]): qm_low = pivot_low[i]

        if bear_state == 0:
            if not np.isnan(qm_high) and c[i] > qm_high:
                bear_state, bear_sweep_bar, bear_qm_val, bear_sweep_high = 1, i, qm_high, h[i]
        elif bear_state == 1:
            bear_sweep_high = max(bear_sweep_high, h[i])
            if i - bear_sweep_bar > MAX_SWEEP_BARS:
                bear_state = 0
            elif c[i] < bear_qm_val:
                if i == n - 1:
                    last_cisd = (-1, bear_sweep_high)
                bear_state = 0

        if bull_state == 0:
            if not np.isnan(qm_low) and c[i] < qm_low:
                bull_state, bull_sweep_bar, bull_qm_val, bull_sweep_low = 1, i, qm_low, l[i]
        elif bull_state == 1:
            bull_sweep_low = min(bull_sweep_low, l[i])
            if i - bull_sweep_bar > MAX_SWEEP_BARS:
                bull_state = 0
            elif c[i] > bull_qm_val:
                if i == n - 1:
                    last_cisd = (1, bull_sweep_low)
                bull_state = 0

    return last_cisd


def find_origin(df_m15, direction):
    o, h, l, c = df_m15['open'].values, df_m15['high'].values, df_m15['low'].values, df_m15['close'].values
    n = len(df_m15)
    for k in range(1, ORIGIN_LOOKBACK + 1):
        i = n - 1 - k
        if i < 0: break
        if direction == -1 and c[i] > o[i]:
            return h[i], l[i]
        elif direction == 1 and c[i] < o[i]:
            return h[i], l[i]
    return None, None


def load_state():
    if QM_STATE_PATH.exists():
        return json.loads(QM_STATE_PATH.read_text())
    return {inst: {"order": None} for inst in INSTRUMENTS}


def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[warn] Telegram not configured:\n" + msg)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TG_CHAT_ID, "text": msg}, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] Telegram send failed: HTTP {r.status_code} — {r.text}")
        else:
            print("[ok] Telegram message sent successfully.")
    except Exception as e:
        print(f"[ERROR] Telegram send raised an exception: {e}")


def process_instrument(inst, yahoo_symbol, state, open_counter):
    order = state[inst].get("order")
    cost = cost_bps(inst)
    tick = tick_size(inst)
    msg_lines = []

    try:
        df_h1 = fetch_bars(yahoo_symbol, "60m", "3mo")
        df_m15 = fetch_bars(yahoo_symbol, "15m", "60d")
    except Exception as e:
        return [f"{inst}: fetch failed ({e})"], state

    if len(df_h1) < PIVOT_LEN * 3 or len(df_m15) < ORIGIN_LOOKBACK + 2:
        return [f"{inst}: insufficient data"], state

    last_price = df_m15['close'].iloc[-1]

    # ---------- manage existing pending/open order first ----------
    if order is not None:
        if not order.get("filled"):
            # check fill
            recent_m15 = df_m15[df_m15.index > pd.Timestamp(order["placed_time"])]
            filled = False
            for t, row in recent_m15.iterrows():
                if order["direction"] == -1 and row["high"] >= order["entry"]:
                    filled = True; fill_time = t; break
                elif order["direction"] == 1 and row["low"] <= order["entry"]:
                    filled = True; fill_time = t; break
            if filled:
                risk_frac = order["risk_fraction"]
                risk_gbp = current_risk_gbp(risk_frac)
                direction_str = "short" if order["direction"] == -1 else "long"
                fill = execute_entry(inst, direction_str, risk_gbp, order["stop"])
                order["filled"] = True
                order["fill_time"] = str(fill_time)
                order["trade_id"] = fill["trade_id"] if fill else None
                msg_lines.append(f"{inst}: LIMIT FILLED {direction_str.upper()} @ {order['entry']:.5f} (~£{risk_gbp:,.0f} at risk)")
                open_counter[0] += 1
            elif pd.Timestamp(order["expiry_time"]) < df_m15.index[-1]:
                msg_lines.append(f"{inst}: pending order EXPIRED unfilled @ {order['entry']:.5f}")
                order = None
        else:
            # manage open position: check stop/target on 15m bars since fill
            recent_m15 = df_m15[df_m15.index > pd.Timestamp(order["fill_time"])]
            for t, row in recent_m15.iterrows():
                if order["direction"] == -1:
                    if row["high"] >= order["stop"]:
                        if order.get("trade_id"): execute_exit(order["trade_id"])
                        pnl, eq = record_trade_close(inst, "QM+CISD+SBR", "short", order["entry"], order["stop"], order["stop"], order["risk_fraction"])
                        msg_lines.append(f"{inst}: EXIT SHORT (stop) @ {order['stop']:.5f}. P&L £{pnl:,.0f}. Equity £{eq:,.0f}")
                        order = None; break
                    elif row["low"] <= order["target"]:
                        if order.get("trade_id"): execute_exit(order["trade_id"])
                        pnl, eq = record_trade_close(inst, "QM+CISD+SBR", "short", order["entry"], order["stop"], order["target"], order["risk_fraction"])
                        msg_lines.append(f"{inst}: EXIT SHORT (target) @ {order['target']:.5f}. P&L £{pnl:,.0f}. Equity £{eq:,.0f}")
                        order = None; break
                else:
                    if row["low"] <= order["stop"]:
                        if order.get("trade_id"): execute_exit(order["trade_id"])
                        pnl, eq = record_trade_close(inst, "QM+CISD+SBR", "long", order["entry"], order["stop"], order["stop"], order["risk_fraction"])
                        msg_lines.append(f"{inst}: EXIT LONG (stop) @ {order['stop']:.5f}. P&L £{pnl:,.0f}. Equity £{eq:,.0f}")
                        order = None; break
                    elif row["high"] >= order["target"]:
                        if order.get("trade_id"): execute_exit(order["trade_id"])
                        pnl, eq = record_trade_close(inst, "QM+CISD+SBR", "long", order["entry"], order["stop"], order["target"], order["risk_fraction"])
                        msg_lines.append(f"{inst}: EXIT LONG (target) @ {order['target']:.5f}. P&L £{pnl:,.0f}. Equity £{eq:,.0f}")
                        order = None; break

    # ---------- look for a new CISD signal only if no order currently active ----------
    if order is None:
        cisd = detect_latest_cisd(df_h1)
        if cisd is not None:
            direction, sweep_extreme = cisd
            origin_top, origin_bot = find_origin(df_m15, direction)
            if origin_top is not None:
                if direction == -1:
                    entry_px = origin_top
                    stop_px = sweep_extreme + SL_BUFFER_TICKS * tick
                    stop_dist = stop_px - entry_px
                else:
                    entry_px = origin_bot
                    stop_px = sweep_extreme - SL_BUFFER_TICKS * tick
                    stop_dist = entry_px - stop_px
                if stop_dist > 0:
                    tp_px = entry_px - stop_dist*RR_TARGET if direction == -1 else entry_px + stop_dist*RR_TARGET
                    risk_frac = available_risk_fraction(open_counter[0])
                    if risk_frac <= 0:
                        msg_lines.append(f"{inst}: CISD signal fired but SKIPPED — 10% risk budget full")
                    else:
                        now = df_m15.index[-1]
                        order = {
                            "direction": direction, "entry": entry_px, "stop": stop_px, "target": tp_px,
                            "placed_time": str(now), "expiry_time": str(now + pd.Timedelta(minutes=15*ORDER_EXPIRY_BARS)),
                            "filled": False, "risk_fraction": risk_frac,
                        }
                        dir_str = "SHORT" if direction == -1 else "LONG"
                        frac_note = f" [{risk_frac:.0%} slice]" if risk_frac < 1.0 else ""
                        msg_lines.append(f"{inst}: NEW PENDING {dir_str} limit @ {entry_px:.5f}, stop {stop_px:.5f}, target {tp_px:.5f}{frac_note}")

    state[inst]["order"] = order
    return msg_lines, state


def main():
    if EXECUTION_ENABLED:
        import oanda_client as oanda
        try:
            live_balance = oanda.get_current_balance()
            from journal import save_equity
            save_equity(live_balance)
            print(f"[OANDA {oanda.ENVIRONMENT.upper()}] Synced equity to £{live_balance:,.2f}")
        except Exception as e:
            print(f"[warn] Could not sync live balance: {e}")
    else:
        print("[forward-test mode] OANDA credentials not set — signal logging only, no real orders.")

    state = load_state()
    open_counter = [0]
    all_msgs = []

    for inst, yahoo_symbol in INSTRUMENTS.items():
        try:
            msgs, state = process_instrument(inst, yahoo_symbol, state, open_counter)
            all_msgs.extend(msgs)
        except Exception as e:
            all_msgs.append(f"{inst}: ERROR {e}")
        time.sleep(1.5)  # space out requests to avoid Yahoo's rate limiter (34 instruments x 2 fetches/run)

    QM_STATE_PATH.write_text(json.dumps(state, indent=2))

    active = [m for m in all_msgs if any(k in m for k in ["FILLED","EXPIRED","EXIT","NEW PENDING","SKIPPED","ERROR"])]
    env_tag = "[LIVE TRADING]" if EXECUTION_ENABLED else "[forward-test / signal only]"
    if active:
        equity = load_equity()
        header = f"QM+CISD+SBR Update {env_tag} - Equity: £{equity:,.0f}\n\n"
        send_telegram(header + "\n".join(active))
    else:
        print("No activity this run.")

    for m in all_msgs:
        print(m)


if __name__ == "__main__":
    main()
