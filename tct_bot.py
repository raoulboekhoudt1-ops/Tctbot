#!/usr/bin/env python3
"""
TCT Alert Bot - v1 (foundation: structure + sweep + SMT scout)

What it does each run:
  1. Pulls recent candles for BTC, ETH, SOL on 15m + 1h from Binance (free, no key).
  2. On the most recently CLOSED 15m candle, looks for:
       - Break of Structure (BOS)  -> close beyond the latest swing high/low
       - Liquidity sweep / SFP      -> wick beyond a swing then close back inside
  3. Adds context: 1h trend bias, premium/discount position, SMT divergence.
  4. Sends a Telegram alert (and remembers what it already alerted, so no spam).

This is a "go look" scout, NOT a TCT model classifier (that's v2) and NOT a trader.
Verify every alert on the chart yourself before doing anything.
"""

import os
import sys
import json
import urllib.request
import urllib.parse
import urllib.error

# ----------------------------- CONFIG ---------------------------------------
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
INTERVALS = {"15m": 120, "1h": 120}   # interval -> how many candles to fetch
PIVOT_K = 2            # a swing needs K candles lower/higher on each side
RANGE_LOOKBACK = 48    # candles used to gauge premium/discount position
STATE_FILE = "state.json"
BINANCE_URL = "https://api.binance.com/api/v3/klines"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ----------------------------- DATA -----------------------------------------
def get_klines(symbol, interval, limit):
    """Fetch klines from Binance. Returns list of dicts of CLOSED candles."""
    params = urllib.parse.urlencode(
        {"symbol": symbol, "interval": interval, "limit": limit}
    )
    req = urllib.request.Request(
        f"{BINANCE_URL}?{params}", headers={"User-Agent": "tct-bot/1.0"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = json.loads(resp.read().decode())
    candles = []
    for k in raw:
        candles.append(
            {
                "open_time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "close_time": int(k[6]),
            }
        )
    # Binance's last candle is still forming -> drop it, keep only closed ones.
    return candles[:-1]


# --------------------------- DETECTION --------------------------------------
def find_pivots(candles, k):
    """Return (swing_high_indices, swing_low_indices) using simple fractals.

    v1 uses fractal pivots (K candles each side). v2 will use the exact
    2-2-2 six-candle rule + MSH/MSL confirmation from the blueprint.
    """
    highs, lows = [], []
    for i in range(k, len(candles) - k):
        window = candles[i - k : i + k + 1]
        c = candles[i]
        if c["high"] == max(w["high"] for w in window) and all(
            c["high"] > candles[j]["high"] for j in range(i - k, i)
        ):
            highs.append(i)
        if c["low"] == min(w["low"] for w in window) and all(
            c["low"] < candles[j]["low"] for j in range(i - k, i)
        ):
            lows.append(i)
    return highs, lows


def latest_swing(indices, candles, field):
    """Most recent pivot value from a list of pivot indices."""
    if not indices:
        return None
    return candles[indices[-1]][field]


def detect_structure_event(candles, k):
    """Look at the last closed candle for a BOS or a sweep vs latest swings."""
    if len(candles) < 2 * k + 3:
        return None
    highs, lows = find_pivots(candles, k)
    last = candles[-1]
    # ignore pivots that ARE the last candle region
    highs = [i for i in highs if i < len(candles) - 1]
    lows = [i for i in lows if i < len(candles) - 1]
    swing_high = latest_swing(highs, candles, "high")
    swing_low = latest_swing(lows, candles, "low")

    # Liquidity sweep / SFP: wick beyond swing but close back inside
    if swing_high is not None and last["high"] > swing_high and last["close"] < swing_high:
        return {"type": "sweep", "side": "high", "level": swing_high}
    if swing_low is not None and last["low"] < swing_low and last["close"] > swing_low:
        return {"type": "sweep", "side": "low", "level": swing_low}

    # Break of Structure: candle CLOSES beyond the swing
    if swing_high is not None and last["close"] > swing_high:
        return {"type": "bos", "side": "bull", "level": swing_high}
    if swing_low is not None and last["close"] < swing_low:
        return {"type": "bos", "side": "bear", "level": swing_low}

    return None


def range_position(candles, lookback):
    """Where price sits in the recent range: premium/discount + label."""
    window = candles[-lookback:] if len(candles) >= lookback else candles
    hi = max(c["high"] for c in window)
    lo = min(c["low"] for c in window)
    if hi == lo:
        return 0.5, "equilibrium"
    pos = (candles[-1]["close"] - lo) / (hi - lo)
    if pos >= 0.75:
        label = "extreme premium"
    elif pos >= 0.55:
        label = "premium"
    elif pos > 0.45:
        label = "equilibrium"
    elif pos > 0.25:
        label = "discount"
    else:
        label = "extreme discount"
    return pos, label


def trend_bias(candles, k):
    """Rough 1h bias from the most recent BOS direction."""
    if len(candles) < 2 * k + 3:
        return "unclear"
    highs, lows = find_pivots(candles, k)
    sh = latest_swing(highs, candles, "high")
    sl = latest_swing(lows, candles, "low")
    last_close = candles[-1]["close"]
    if sh is not None and last_close > sh:
        return "bullish"
    if sl is not None and last_close < sl:
        return "bearish"
    return "ranging"


def swing_label(candles, k):
    """Classify the last swing high as HH/LH and last low as LL/HL (for SMT)."""
    highs, lows = find_pivots(candles, k)
    hh = lh = ll = hl = None
    if len(highs) >= 2:
        hh = candles[highs[-1]]["high"] > candles[highs[-2]]["high"]
    if len(lows) >= 2:
        hl = candles[lows[-1]]["low"] > candles[lows[-2]]["low"]
    return {"higher_high": hh, "higher_low": hl}


def detect_smt(swing_labels):
    """SMT divergence: if assets disagree on HH (bearish) or HL (bullish)."""
    notes = []
    syms = list(swing_labels.keys())
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            a, b = syms[i], syms[j]
            la, lb = swing_labels[a], swing_labels[b]
            if la["higher_high"] is not None and lb["higher_high"] is not None:
                if la["higher_high"] != lb["higher_high"]:
                    strong = a if la["higher_high"] else b
                    weak = b if la["higher_high"] else a
                    notes.append(
                        f"bearish SMT: {strong} made a higher high but {weak} didn't"
                    )
            if la["higher_low"] is not None and lb["higher_low"] is not None:
                if la["higher_low"] != lb["higher_low"]:
                    strong = a if la["higher_low"] else b
                    weak = b if la["higher_low"] else a
                    notes.append(
                        f"bullish SMT: {weak} made a lower low but {strong} held higher"
                    )
    return notes


# --------------------------- TELEGRAM ---------------------------------------
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram secrets missing; printing instead:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    ).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            r.read()
    except urllib.error.URLError as e:
        print(f"[ERROR] Telegram send failed: {e}")


# ----------------------------- STATE ----------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ----------------------------- MAIN -----------------------------------------
def run():
    state = load_state()
    first_ever_run = "initialised" not in state

    # Fetch everything first so SMT can compare across symbols.
    data = {}
    for sym in SYMBOLS:
        data[sym] = {}
        for itv in INTERVALS:
            try:
                data[sym][itv] = get_klines(sym, itv, INTERVALS[itv])
            except Exception as e:  # noqa: BLE001
                print(f"[ERROR] fetch {sym} {itv}: {e}")
                data[sym][itv] = []

    # SMT across symbols on the 15m timeframe.
    swing_labels = {
        s: swing_label(data[s]["15m"], PIVOT_K) for s in SYMBOLS if data[s]["15m"]
    }
    smt_notes = detect_smt(swing_labels) if len(swing_labels) >= 2 else []

    if first_ever_run:
        send_telegram(
            "✅ <b>TCT scout is live</b>\n"
            "Watching BTC / ETH / SOL on 15m (with 1h context).\n"
            "I'll ping you on a break of structure or a liquidity sweep.\n"
            "<i>v1 = mechanical precursors, not full model detection. Always verify.</i>"
        )
        state["initialised"] = True

    for sym in SYMBOLS:
        c15 = data[sym]["15m"]
        c1h = data[sym]["1h"]
        if not c15:
            continue

        event = detect_structure_event(c15, PIVOT_K)
        if not event:
            continue

        last = c15[-1]
        key = f"{sym}_15m_{event['type']}_{event['side']}"
        if state.get(key) == last["close_time"]:
            continue  # already alerted on this exact candle's event

        bias = trend_bias(c1h, PIVOT_K) if c1h else "unclear"
        pos, pos_label = range_position(c15, RANGE_LOOKBACK)

        if event["type"] == "bos":
            arrow = "🟢" if event["side"] == "bull" else "🔴"
            headline = f"{arrow} <b>{sym}</b> 15m — {event['side'].upper()} break of structure"
        else:
            side = "buy-side" if event["side"] == "high" else "sell-side"
            headline = f"💧 <b>{sym}</b> 15m — {side} liquidity sweep"

        lines = [
            headline,
            f"price: {last['close']:.2f}  ({pos_label} of recent range)",
            f"1h bias: {bias}",
        ]
        if smt_notes:
            lines.append("SMT: " + "; ".join(smt_notes))
        lines.append("→ go check the chart for a TCT model before acting.")

        send_telegram("\n".join(lines))
        state[key] = last["close_time"]

    save_state(state)
    print("Run complete.")


# --------------------------- SELF TEST --------------------------------------
def selftest():
    """Run detection on synthetic candles (no network) to sanity-check logic."""
    def mk(o, h, l, c, t):
        return {"open": o, "high": h, "low": l, "close": c,
                "open_time": t, "close_time": t + 1}

    # Build a series with a clear swing high then a bullish break above it.
    seq = [100, 101, 103, 102, 100, 99, 101, 104, 106, 105, 103, 108]
    candles = []
    for i, price in enumerate(seq):
        candles.append(mk(price - 0.5, price + 1, price - 1, price, i * 1000))

    print("pivots:", find_pivots(candles, 2))
    print("structure event:", detect_structure_event(candles, 2))
    print("range position:", range_position(candles, 10))
    print("trend bias:", trend_bias(candles, 2))
    print("swing label:", swing_label(candles, 2))

    # Sweep example: wick above prior swing high but close back below.
    sweep = candles[:-1] + [mk(106, 112, 105, 106, 20000)]
    print("sweep event:", detect_structure_event(sweep, 2))

    # SMT example across two fake symbols.
    labels = {
        "BTCUSDT": {"higher_high": True, "higher_low": True},
        "ETHUSDT": {"higher_high": False, "higher_low": True},
    }
    print("smt:", detect_smt(labels))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        run()
