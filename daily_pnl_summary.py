"""
Daily P&L summary — reads journal.csv (shared across all 3 strategies:
NAS100, EURGBP, ADX+Supertrend x12) and sends one end-of-day digest to
Telegram. Run once per day, after the other two jobs have had a chance
to run for the day.

Does not place trades or modify the journal — read-only summary.

Requires env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import os
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE = Path(__file__).parent
JOURNAL_PATH = BASE / "journal.csv"
EQUITY_PATH = BASE / "equity.json"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

STARTING_EQUITY = 1_000_000.0


def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[warn] Telegram not configured:\n" + msg)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"})


def main():
    if not JOURNAL_PATH.exists():
        send_telegram("Daily P&L: journal.csv not found yet — no trades logged.")
        return

    journal = pd.read_csv(JOURNAL_PATH, parse_dates=["close_timestamp"])

    if journal.empty:
        current_equity = STARTING_EQUITY
    else:
        current_equity = journal["equity_after"].iloc[-1]

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_trades = journal[journal["close_timestamp"] >= today_start] if not journal.empty else journal

    total_pnl_all_time = current_equity - STARTING_EQUITY
    total_return_pct = (current_equity / STARTING_EQUITY - 1) * 100

    lines = [f"*Daily P&L Summary — {today_start.strftime('%Y-%m-%d')}*\n"]

    if today_trades.empty:
        lines.append("No trades closed today.")
    else:
        today_pnl = today_trades["pnl_gbp"].sum()
        n_trades = len(today_trades)
        wins = (today_trades["pnl_gbp"] > 0).sum()
        losses = (today_trades["pnl_gbp"] < 0).sum()

        lines.append(f"Trades closed today: {n_trades} ({wins}W / {losses}L)")
        lines.append(f"Today's P&L: £{today_pnl:,.0f}")
        lines.append("")
        lines.append("By trade:")
        for _, row in today_trades.iterrows():
            sign = "+" if row["pnl_gbp"] >= 0 else ""
            lines.append(f"  {row['instrument']} ({row['strategy']}): {sign}£{row['pnl_gbp']:,.0f} ({row['r_multiple']:+.2f}R)")

    lines.append("")
    lines.append(f"*Current equity: £{current_equity:,.0f}*")
    lines.append(f"All-time P&L: £{total_pnl_all_time:+,.0f} ({total_return_pct:+.2f}%)")

    msg = "\n".join(lines)
    send_telegram(msg)
    print(msg)


if __name__ == "__main__":
    main()
