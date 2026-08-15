"""
compass/eurusd_data.py -- EURUSD (C:EURUSD) data layer for the Session-2 baseline backtest.
Self-contained fetch + cache (does NOT touch the Gold polygon_fetcher). Same Polygon Currencies Starter plan.
Data infrastructure only -- not wired to any live system.
"""
import csv
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

try:
    from . import polygon_fetcher as pf   # reuse the key loader + data dir only
except ImportError:
    import polygon_fetcher as pf

TICKER = "C:EURUSD"
BASE_URL = "https://api.polygon.io"
DATA_DIR = pf.DATA_DIR
CSV_COLS = ["timestamp", "open", "high", "low", "close", "volume"]
TIMEFRAMES = {"5min": (5, "minute", "eurusd_5min.csv"),
              "1hour": (1, "hour", "eurusd_1hour.csv"),
              "daily": (1, "day", "eurusd_daily.csv")}
BACKFILL_DAYS = 730


def _fetch(mult, span, frm, to):
    key = pf._api_key()
    url = "%s/v2/aggs/ticker/%s/range/%d/%s/%s/%s" % (BASE_URL, TICKER, mult, span, frm, to)
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key}
    out, pages = [], 0
    while url and pages < 20:
        d = requests.get(url, params=params, timeout=60).json()
        out.extend(d.get("results") or [])
        url = d.get("next_url")
        if not url:
            break
        params = {"apiKey": key}; pages += 1; time.sleep(0.05)
    return out


def ensure_data(force: bool = False) -> dict:
    """Backfill 2yr of Silver 5m/1h/daily to CSV if not already present. Returns per-tf counts."""
    res = {}
    frm = (datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)).strftime("%Y-%m-%d")
    to = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for tf, (mult, span, name) in TIMEFRAMES.items():
        p = DATA_DIR / name
        if p.exists() and not force:
            res[tf] = ("cached", sum(1 for _ in open(p, encoding="utf-8")) - 1)
            continue
        rows = []
        seen = set()
        for c in sorted(_fetch(mult, span, frm, to), key=lambda x: x["t"]):
            t = int(c["t"])
            if t in seen:
                continue
            seen.add(t)
            ts = datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append({"timestamp": ts, "open": c.get("o"), "high": c.get("h"),
                         "low": c.get("l"), "close": c.get("c"), "volume": c.get("v", 0)})
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLS); w.writeheader(); w.writerows(rows)
        res[tf] = ("backfill", len(rows))
    return res


def load(tf: str) -> list:
    p = DATA_DIR / TIMEFRAMES[tf][2]
    out = []
    with open(p, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                out.append({"timestamp": r["timestamp"], "open": float(r["open"]), "high": float(r["high"]),
                            "low": float(r["low"]), "close": float(r["close"]),
                            "volume": float(r["volume"]) if r.get("volume") not in (None, "") else 0.0})
            except (TypeError, ValueError):
                continue
    return out


if __name__ == "__main__":
    for tf, (action, n) in ensure_data().items():
        print("  %-6s %-9s %d candles" % (tf, action, n))
