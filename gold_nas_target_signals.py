"""
Daily forward-test signal logger — Level Continuation strategy for
XAUUSD (Gold) and NAS100, with 1% risk-based journaling. See journal.py
for the shared risk/R-multiple methodology.

Strategy source: ported from the "KenJTradez" Pine v6 indicator's Proj H/L
bands and its own "Trade Idea" panel (Long -> Proj H, Short -> Proj L,
SL = 1.5x ATR(14)). The indicator computes, from a rolling 75-trading-day
lookback:
  up_median / up_p75 / up_p90     = percentiles of (High - Open) / Open
  down_median / down_p75 / down_p90 = percentiles of (Open - Low) / Open
applied to TODAY's open to project six levels above/below it. This is the
exact same "up_*/down_*" naming used in reversal_backtest.json, which
measured how often price *reverses* at these levels. This script trades
the opposite hypothesis: continuation through them.

Entry trigger (this script's own design choice, not in the indicator):
  - today's close breaks beyond the MEDIAN level -> enter in that direction
  - target = the 75th-percentile level (the indicator's own "Long/Short ->
    Proj H/L 75p" trade idea row)
  - stop = 1.5x ATR(14) from the entry close (the indicator's own SL rule)
Only one target is used (not a two-stage median->p75->p90 scale-out) to
keep this compatible with journal.py's single-fill trade model, same as
every other strategy in this repo.

KNOWN LIMITATION: daily OHLC bars can't tell us which of target/stop was
hit first if both fall within the same day's high-low range. This script
resolves that conservatively by checking the stop FIRST (worst case).

Requires env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Data source: Yahoo Finance (^NDX for NAS100, XAUUSD=X for Gold, matching
the tickers already used elsewhere in this repo).
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

LOG_PATH = BASE / "gold_nas_target_log.csv"
STATE_PATH = BASE / "gold_nas_target_state.json"

LOOKBACK = 75          # trading days — matches the indicator's default lookback input
MIN_HISTORY_DAYS = 20  # don't trade until at least this many history days are available
ATR_STOP_MULT = 1.5    # matches the indicator's own "Trade Idea" SL sizing

INSTRUMENTS = {
    "NAS100": "^NDX",
    "XAUUSD": "XAUUSD=X",
}

DEFAULT_POSITION = {
    "state": 0, "direction": None, "entry_price": None,
    "target_price": None, "stop_price": None,
    "entry_date": None, "last_processed_date": None,
}


def fetch_daily_bars(symbol, range_):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": range_, "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"Yahoo error for {symbol}: {data}")
    result = result[0]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    rows = []
    for i in range(len(timestamps)):
        o, h, l, c = quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i]
        if None in (o, h, l, c):
            continue
        date = pd.to_datetime(timestamps[i], unit="s").strftime("%Y-%m-%d")
        rows.append({"date": date, "open": float(o), "high": float(h), "low": float(l), "close": float(c)})
    return rows


def load_or_seed_log():
    if LOG_PATH.exists():
        df = pd.read_csv(LOG_PATH, parse_dates=["date"])
    else:
        df = pd.DataFrame(columns=["date", "instrument", "open", "high", "low", "close"])

    frames = [df]
    for key, symbol in INSTRUMENTS.items():
        existing = df[df["instrument"] == key] if not df.empty else df
        # Seed with 2y of history on first run so the 75-day lookback is
        # immediately usable; otherwise just top up the last month.
        range_ = "2y" if existing.empty else "1mo"
        rows = fetch_daily_bars(symbol, range_)
        existing_dates = set(existing["date"].dt.strftime("%Y-%m-%d")) if not existing.empty else set()
        new_rows = [{**r, "instrument": key} for r in rows if r["date"] not in existing_dates]
        if new_rows:
            frames.append(pd.DataFrame(new_rows).assign(date=lambda d: pd.to_datetime(d["date"])))

    if len(frames) > 1:
        df = pd.concat(frames, ignore_index=True)
        df = df.sort_values(["instrument", "date"]).drop_duplicates(["instrument", "date"], keep="last").reset_index(drop=True)
    return df


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {k: dict(DEFAULT_POSITION) for k in INSTRUMENTS}


def atr14(instrument_df):
    h, l, c = instrument_df["high"], instrument_df["low"], instrument_df["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(14).mean().iloc[-1]


def compute_levels(history_df):
    """history_df: an instrument's daily bars, NOT including today. Returns
    the six up_*/down_* fractional excursions, percentile-interpolated
    exactly like the Pine indicator's array.percentile_linear_interpolation."""
    df = history_df.copy()
    flat = (df["high"] == df["low"]) & (df["close"] == df["open"])
    df = df[~flat]
    if df.empty:
        return None
    up = (df["high"] - df["open"]) / df["open"]
    down = (df["open"] - df["low"]) / df["open"]

    def pct(series, q):
        return float(np.percentile(series.tail(LOOKBACK), q, method="linear"))

    return {
        "up_median": pct(up, 50), "up_p75": pct(up, 75), "up_p90": pct(up, 90),
        "down_median": pct(down, 50), "down_p75": pct(down, 75), "down_p90": pct(down, 90),
        "n": min(len(df), LOOKBACK),
    }


def process_instrument(key, df, state, msgs):
    inst_df = df[df["instrument"] == key].sort_values("date").reset_index(drop=True)
    if inst_df.empty:
        msgs.append(f"{key}: no data")
        return state

    latest = inst_df.iloc[-1]
    today_date = latest["date"].strftime("%Y-%m-%d")
    s = state.get(key, dict(DEFAULT_POSITION))

    if s.get("last_processed_date") == today_date:
        msgs.append(f"{key}: already processed {today_date}")
        return state

    history = inst_df.iloc[:-1]
    if len(history) < MIN_HISTORY_DAYS:
        s["last_processed_date"] = today_date
        state[key] = s
        msgs.append(f"{key}: insufficient history ({len(history)} days)")
        return state

    today_open, today_high = latest["open"], latest["high"]
    today_low, today_close = latest["low"], latest["close"]
    atr = atr14(inst_df)

    if s["state"] == 0:
        levels = compute_levels(history)
        up_median_px = today_open * (1 + levels["up_median"])
        up_p75_px = today_open * (1 + levels["up_p75"])
        up_p90_px = today_open * (1 + levels["up_p90"])
        down_median_px = today_open * (1 - levels["down_median"])
        down_p75_px = today_open * (1 - levels["down_p75"])
        down_p90_px = today_open * (1 - levels["down_p90"])

        # Pick the next level still AHEAD of today's close as the target —
        # a close that already gapped past p75 targets p90 instead of a
        # target that sits behind the entry. A close already past p90 has
        # no further defined level, so it's skipped rather than faked.
        if today_close >= up_median_px:
            target = up_p75_px if today_close < up_p75_px else (up_p90_px if today_close < up_p90_px else None)
            if target is not None:
                entry = today_close
                stop = entry - ATR_STOP_MULT * atr if not np.isnan(atr) else entry * 0.99
                s.update({"state": 1, "direction": "long", "entry_price": entry,
                          "target_price": target, "stop_price": stop, "entry_date": today_date})
                risk_gbp = current_risk_gbp()
                msgs.append(f"*{key}* — ENTER LONG @ {entry:,.2f} (target {target:,.2f}, stop {stop:,.2f}, "
                            f"~£{risk_gbp:,.0f} at risk, {levels['n']}d sample)")
            else:
                msgs.append(f"{key}: close {today_close:,.2f} already past the 90th-pct level ({up_p90_px:,.2f}) — no defined target, skipping entry")
        elif today_close <= down_median_px:
            target = down_p75_px if today_close > down_p75_px else (down_p90_px if today_close > down_p90_px else None)
            if target is not None:
                entry = today_close
                stop = entry + ATR_STOP_MULT * atr if not np.isnan(atr) else entry * 1.01
                s.update({"state": -1, "direction": "short", "entry_price": entry,
                          "target_price": target, "stop_price": stop, "entry_date": today_date})
                risk_gbp = current_risk_gbp()
                msgs.append(f"*{key}* — ENTER SHORT @ {entry:,.2f} (target {target:,.2f}, stop {stop:,.2f}, "
                            f"~£{risk_gbp:,.0f} at risk, {levels['n']}d sample)")
            else:
                msgs.append(f"{key}: close {today_close:,.2f} already past the 90th-pct level ({down_p90_px:,.2f}) — no defined target, skipping entry")
        else:
            msgs.append(f"{key}: FLAT @ {today_close:,.2f} (median levels {down_median_px:,.2f} / {up_median_px:,.2f})")

    elif s["state"] == 1:  # long
        if today_low <= s["stop_price"]:  # stop checked first — see module docstring
            pnl, eq = record_trade_close(key, "Level Continuation", "long", s["entry_price"], s["stop_price"], s["stop_price"])
            msgs.append(f"*{key}* — EXIT LONG @ {s['stop_price']:,.2f} (stopped out). P&L £{pnl:,.0f}. Equity £{eq:,.0f}")
            s = dict(DEFAULT_POSITION)
        elif today_high >= s["target_price"]:
            pnl, eq = record_trade_close(key, "Level Continuation", "long", s["entry_price"], s["stop_price"], s["target_price"])
            msgs.append(f"*{key}* — EXIT LONG @ {s['target_price']:,.2f} (target hit). P&L £{pnl:,.0f}. Equity £{eq:,.0f}")
            s = dict(DEFAULT_POSITION)
        else:
            msgs.append(f"{key}: HOLD LONG @ {today_close:,.2f} (target {s['target_price']:,.2f}, stop {s['stop_price']:,.2f})")

    elif s["state"] == -1:  # short
        if today_high >= s["stop_price"]:  # stop checked first — see module docstring
            pnl, eq = record_trade_close(key, "Level Continuation", "short", s["entry_price"], s["stop_price"], s["stop_price"])
            msgs.append(f"*{key}* — EXIT SHORT @ {s['stop_price']:,.2f} (stopped out). P&L £{pnl:,.0f}. Equity £{eq:,.0f}")
            s = dict(DEFAULT_POSITION)
        elif today_low <= s["target_price"]:
            pnl, eq = record_trade_close(key, "Level Continuation", "short", s["entry_price"], s["stop_price"], s["target_price"])
            msgs.append(f"*{key}* — EXIT SHORT @ {s['target_price']:,.2f} (target hit). P&L £{pnl:,.0f}. Equity £{eq:,.0f}")
            s = dict(DEFAULT_POSITION)
        else:
            msgs.append(f"{key}: HOLD SHORT @ {today_close:,.2f} (target {s['target_price']:,.2f}, stop {s['stop_price']:,.2f})")

    s["last_processed_date"] = today_date
    state[key] = s
    return state


def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[warn] Telegram not configured:\n" + msg)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"})


def main():
    df = load_or_seed_log()
    state = load_state()
    msgs = []

    for key in INSTRUMENTS:
        try:
            state = process_instrument(key, df, state, msgs)
        except Exception as e:
            msgs.append(f"{key}: ERROR: {e}")

    trimmed = [df[df["instrument"] == k].sort_values("date").tail(LOOKBACK + 30) for k in INSTRUMENTS]
    df_out = pd.concat(trimmed, ignore_index=True) if trimmed else df
    df_out.to_csv(LOG_PATH, index=False)
    STATE_PATH.write_text(json.dumps(state, indent=2))

    equity = load_equity()
    header = f"*Level Continuation (Gold/NAS100) — Equity: £{equity:,.0f}*\n\n"
    full_msg = header + "\n".join(msgs)
    send_telegram(full_msg)
    print(full_msg)


if __name__ == "__main__":
    main()
