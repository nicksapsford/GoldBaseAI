"""
compass/swing_levels.py -- Merlin's Compass swing-level (support/resistance) detection (Session 1).
================================================================================
Reads the cached Gold 1-hour candles and finds recent swing highs (resistance) and swing lows (support).

DATA INFRASTRUCTURE ONLY -- not wired to Lancelot. (Gaius Commission 026 -> Merlin's Compass.)

Definitions (from the brief):
  * swing HIGH: a candle whose HIGH exceeds the highs of the 3 candles before AND the 3 candles after it.
  * swing LOW : a candle whose LOW  is below the lows  of the 3 candles before AND the 3 candles after it.
  * lookback  : the last 72 one-hour candles.
Resistance = swing highs ABOVE the current price; support = swing lows BELOW it, each nearest-first.
Always returns >=1 of each; falls back to the lookback session high/low when swings are sparse.
"""
from pathlib import Path

try:
    from . import polygon_fetcher as pf
except ImportError:                       # allow running as a plain script
    import polygon_fetcher as pf

WINDOW = 3          # candles required on each side of a swing
LOOKBACK = 72       # one-hour candles to scan
_CLUSTER_PTS = 2.0  # merge swing levels within this many points so the 3 reported levels are distinct


def _cluster(levels: list, reverse: bool) -> list:
    """Sort and merge near-equal levels (within _CLUSTER_PTS) so we don't report 3 duplicates of one level."""
    out = []
    for lv in sorted(levels, reverse=reverse):
        if not out or abs(lv - out[-1]) > _CLUSTER_PTS:
            out.append(lv)
    return out


def detect(current_price: float = None, rows: list = None) -> dict:
    """Return the S/R structure dict (see brief). `rows` may be injected for testing/backtests; otherwise the
    cached 1-hour CSV is used. `current_price` defaults to the last 1-hour close."""
    if rows is None:
        rows = pf.load("1hour")
    window = rows[-LOOKBACK:] if len(rows) > LOOKBACK else rows
    if not window:
        raise RuntimeError("no 1-hour candles available -- run polygon_fetcher.update('1hour') first")

    if current_price is None:
        current_price = window[-1]["close"]

    swing_highs, swing_lows = [], []
    n = len(window)
    for i in range(WINDOW, n - WINDOW):
        h = window[i]["high"]
        if all(h > window[i - j]["high"] for j in range(1, WINDOW + 1)) and \
           all(h > window[i + j]["high"] for j in range(1, WINDOW + 1)):
            swing_highs.append(round(h, 2))
        lo = window[i]["low"]
        if all(lo < window[i - j]["low"] for j in range(1, WINDOW + 1)) and \
           all(lo < window[i + j]["low"] for j in range(1, WINDOW + 1)):
            swing_lows.append(round(lo, 2))

    # resistance = swings above price (nearest first = ascending); support = swings below (nearest = descending)
    resistance = _cluster([h for h in swing_highs if h > current_price], reverse=False)[:3]
    support = _cluster([lo for lo in swing_lows if lo < current_price], reverse=True)[:3]

    # fallback to the lookback session high/low so we always have >=1 of each
    sess_high = round(max(r["high"] for r in window), 2)
    sess_low = round(min(r["low"] for r in window), 2)
    if not resistance:
        resistance = [sess_high] if sess_high > current_price else [round(current_price, 2)]
    if not support:
        support = [sess_low] if sess_low < current_price else [round(current_price, 2)]

    nearest_resistance = resistance[0]
    nearest_support = support[0]
    return {
        "timestamp": window[-1]["timestamp"],
        "instrument": "GOLD",
        "current_price": round(current_price, 2),
        "resistance_levels": resistance,
        "support_levels": support,
        "nearest_resistance": nearest_resistance,
        "nearest_support": nearest_support,
        "upside_to_resistance": round(nearest_resistance - current_price, 2),
        "downside_to_support": round(current_price - nearest_support, 2),
        "swing_highs_found": len(swing_highs),
        "swing_lows_found": len(swing_lows),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(detect(), indent=2))
