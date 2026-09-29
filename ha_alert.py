"""
Heikin Ashi 4H flip alert bot.

Fetches candles from Binance's public API, computes Heikin Ashi values,
and sends a Telegram push notification whenever the most recently CLOSED
4H HA candle has a different color (green/red) than the last color we
successfully confirmed.

State only advances once Telegram confirms the message was delivered
(HTTP 200 + "ok": true). If a send fails for any reason, the previous
color stays recorded, so the very next run retries the same flip instead
of silently losing it.
"""

import json
import os
import sys
from pathlib import Path

import requests

# ---- Configuration ----------------------------------------------------

SYMBOLS = ["ZECUSDT", "SOLUSDT"]
INTERVAL = "1m"
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
    The LAST item in the input klines list is the still-forming candle.
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


def send_telegram_message(text: str) -> bool:
    """Return True only if Telegram confirms delivery, so the caller can
    decide whether it's safe to advance state."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID env vars.", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, data=payload, timeout=15)
    except Exception as e:
        print(f"Telegram send raised an exception: {e}", file=sys.stderr)
        return False

    if resp.status_code != 200:
        print(f"Telegram send failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return False

    ok = resp.json().get("ok", False)
    if not ok:
        print(f"Telegram reported failure: {resp.text}", file=sys.stderr)
    return ok


# ---- Main -------------------------------------------------------------------


def check_symbol(symbol: str, state: dict):
    klines = fetch_klines(symbol)
    ha_candles = compute_heikin_ashi(klines)

    if len(ha_candles) < 2:
        print(f"{symbol}: not enough data yet.")
        return

    # Last item is the still-forming candle; the one before it is the most
    # recently CLOSED candle, which is what we judge color against.
    last_closed = ha_candles[-2]

    sym_state = state.get(symbol, {})
    last_confirmed_color = sym_state.get("last_confirmed_color")
    last_confirmed_open_time = sym_state.get("last_confirmed_open_time")

    # First run ever for this symbol: just record a baseline, no alert.
    if last_confirmed_color is None:
        print(f"{symbol}: first run, recording baseline color={last_closed['color']}")
        state[symbol] = {
            "last_confirmed_color": last_closed["color"],
            "last_confirmed_open_time": last_closed["open_time"],
        }
        return

    # Already confirmed this exact candle before (normal, no-op case).
    if last_confirmed_open_time == last_closed["open_time"]:
        print(f"{symbol}: last={last_closed['color']} flipped=False (already confirmed)")
        return

    flipped = last_closed["color"] != last_confirmed_color
    print(
        f"{symbol}: prev_confirmed={last_confirmed_color} last={last_closed['color']} "
        f"flipped={flipped}"
    )

    if not flipped:
        # Color unchanged; just move the confirmed pointer forward to this candle.
        state[symbol] = {
            "last_confirmed_color": last_closed["color"],
            "last_confirmed_open_time": last_closed["open_time"],
        }
        return

    emoji = "🟢" if last_closed["color"] == "green" else "🔴"
    msg = (
        f"{emoji} *{symbol}* Heikin Ashi flipped to *{last_closed['color'].upper()}* "
        f"on the 4H timeframe.\n"
        f"HA Open: {last_closed['ha_open']:.4f}\n"
        f"HA Close: {last_closed['ha_close']:.4f}"
    )
    sent_ok = send_telegram_message(msg)

    if sent_ok:
        state[symbol] = {
            "last_confirmed_color": last_closed["color"],
            "last_confirmed_open_time": last_closed["open_time"],
        }
        print(f"{symbol}: alert sent and confirmed.")
    else:
        # Do NOT advance state. Next run will see the same unresolved flip
        # and try again, instead of losing it silently.
        print(f"{symbol}: alert send FAILED, will retry next run.", file=sys.stderr)


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
