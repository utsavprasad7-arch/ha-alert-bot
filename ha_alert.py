"""
Heikin Ashi 4H flip alert bot.

Fetches candles from Binance's public API, computes Heikin Ashi values,
and sends a Telegram push notification whenever the most recently CLOSED
4H HA candle has a different color (green/red) than the one before it.

State (which candle we've already alerted on) is stored in state.json so
the same flip is never reported twice, even if this script runs more
often than the candle interval.
"""

import json
import os
import sys
from pathlib import Path

import requests

# ---- Configuration ----------------------------------------------------

SYMBOLS = ["ZECUSDT", "SOLUSDT"]
INTERVAL = "4h"
KLINES_LIMIT = 100  # history needed to seed the HA recursion accurately
BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"

STATE_FILE = Path(__file__).parent / "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ---- Binance ------------------------------------------------------------


def fetch_klines(symbol: str, interval: str = INTERVAL, limit: int = KLINES_LIMIT):
    """Return raw kline data from Binance's public REST API (no key needed)."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(BINANCE_URL, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ---- Heikin Ashi --------------------------------------------------------


def compute_heikin_ashi(klines):
    """
    Convert raw Binance klines into a list of Heikin Ashi candles.
    Each item: {open_time, ha_open, ha_close, color}
    The LAST item in the input klines list is the still-forming candle,
    so we compute HA for everything but treat the final one as "open".
    """
    ha_candles = []
    prev_ha_open = None
    prev_ha_close = None

    for k in klines:
        open_time = k[0]
        o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])

        ha_close = (o + h + l + c) / 4.0

        if prev_ha_open is None:
            ha_open = (o + c) / 2.0
        else:
            ha_open = (prev_ha_open + prev_ha_close) / 2.0

        color = "green" if ha_close >= ha_open else "red"

        ha_candles.append(
            {
                "open_time": open_time,
                "ha_open": ha_open,
                "ha_close": ha_close,
                "color": color,
            }
        )

        prev_ha_open = ha_open
        prev_ha_close = ha_close

    return ha_candles


# ---- State ----------------------------------------------------------------


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---- Telegram ---------------------------------------------------------------


def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID env vars.", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    resp = requests.post(url, data=payload, timeout=15)
    if resp.status_code != 200:
        print(f"Telegram send failed: {resp.status_code} {resp.text}", file=sys.stderr)


# ---- Main -------------------------------------------------------------------


def check_symbol(symbol: str, state: dict):
    klines = fetch_klines(symbol)
    ha_candles = compute_heikin_ashi(klines)

    # The last candle in the list is still forming (not closed yet), so we
    # compare the two most recently CLOSED candles: index -2 and -3.
    if len(ha_candles) < 3:
        print(f"{symbol}: not enough data yet.")
        return

    last_closed = ha_candles[-2]
    prev_closed = ha_candles[-3]

    last_notified_open_time = state.get(symbol, {}).get("last_notified_open_time")

    flipped = last_closed["color"] != prev_closed["color"]
    already_notified = last_notified_open_time == last_closed["open_time"]

    print(
        f"{symbol}: prev={prev_closed['color']} last={last_closed['color']} "
        f"flipped={flipped} already_notified={already_notified}"
    )

    if flipped and not already_notified:
        emoji = "🟢" if last_closed["color"] == "green" else "🔴"
        msg = (
            f"{emoji} *{symbol}* Heikin Ashi flipped to *{last_closed['color'].upper()}* "
            f"on the 4H timeframe.\n"
            f"HA Open: {last_closed['ha_open']:.4f}\n"
            f"HA Close: {last_closed['ha_close']:.4f}"
        )
        send_telegram_message(msg)

    state[symbol] = {"last_notified_open_time": last_closed["open_time"]}


def main():
    state = load_state()
    for symbol in SYMBOLS:
        try:
            check_symbol(symbol, state)
        except Exception as e:
            print(f"Error processing {symbol}: {e}", file=sys.stderr)
    save_state(state)


if __name__ == "__main__":
    main()
