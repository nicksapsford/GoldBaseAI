"""
compass/polygon_fetcher.py -- Merlin's Compass data layer (Session 1, 14 Aug 2026).
================================================================================
Fetches Gold (C:XAUUSD) candles from Polygon.io at three timeframes and caches them locally as CSV so the
Lancelot poll cycle reads from disk instead of the API on every tick.

DATA INFRASTRUCTURE ONLY -- this module is NOT wired into Lancelot, Stanley or any live-trading path. Importing
or running it has zero effect on GoldBase trading. (Gaius Commission 026 -> Merlin's Compass.)

Key gotchas baked in (confirmed on the live plan, 14 Aug 2026):
  * always request limit=50000 AND follow next_url -- 2 years of 5-min data (~140k candles) exceeds one page.
  * the API key loads from the AlbionBase-root backtest_secrets.env; it is NEVER hardcoded or logged.
"""
import csv
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

TICKER = "C:XAUUSD"
BASE_URL = "https://api.polygon.io"

_ROOT = Path(__file__).resolve().parents[2]          # ...\AlbionBase (root, not a git repo)
_SECRETS = _ROOT / "backtest_secrets.env"
DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# timeframe -> Polygon (multiplier, timespan) + local CSV name
TIMEFRAMES = {
    "5min":  {"mult": 5, "span": "minute", "csv": "gold_5min.csv"},
    "1hour": {"mult": 1, "span": "hour",   "csv": "gold_1hour.csv"},
    "daily": {"mult": 1, "span": "day",    "csv": "gold_daily.csv"},
}
CSV_COLS = ["timestamp", "open", "high", "low", "close", "volume"]
FRESH_SECS = 10 * 60          # cached CSV younger than this -> skip the API (brief: <10 min old = fresh)
BACKFILL_DAYS = 730           # 2 years initial backfill
_MAX_PAGES = 50               # safety cap on pagination


# ── key ───────────────────────────────────────────────────────────────────────
def _api_key() -> str:
    load_dotenv(_SECRETS)
    k = (os.getenv("POLYGON_API_KEY") or "").strip()
    if not k:
        raise RuntimeError("POLYGON_API_KEY not found in %s" % _SECRETS)
    return k


# ── low-level fetch (paginated) ───────────────────────────────────────────────
def _fetch_range(key: str, mult: int, span: str, frm, to) -> list:
    """Return all candles for [frm, to] (date strings YYYY-MM-DD or ms ints), following next_url pagination.
    Each candle is a dict: {t(ms), o, h, l, c, v}."""
    url = "%s/v2/aggs/ticker/%s/range/%d/%s/%s/%s" % (BASE_URL, TICKER, mult, span, frm, to)
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key}
    out, pages = [], 0
    while url and pages < _MAX_PAGES:
        r = requests.get(url, params=params, timeout=60)
        r.raise_for_status()
        d = r.json()
        if d.get("status") not in ("OK", "DELAYED") and not d.get("results"):
            # surface a real API error (bad key, entitlement) rather than silently returning nothing
            if d.get("error"):
                raise RuntimeError("Polygon error: %s" % d["error"])
        out.extend(d.get("results") or [])
        nxt = d.get("next_url")
        if not nxt:
            break
        url, params, pages = nxt, {"apiKey": key}, pages + 1   # next_url is fully-formed; only add the key
        time.sleep(0.05)
    return out


def _candles_to_rows(candles: list) -> list:
    """Polygon candle dicts -> our CSV row dicts, sorted by time, de-duplicated on timestamp."""
    seen, rows = set(), []
    for c in sorted(candles, key=lambda x: x["t"]):
        t = int(c["t"])
        if t in seen:
            continue
        seen.add(t)
        ts = datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append({"timestamp": ts, "open": c.get("o"), "high": c.get("h"),
                     "low": c.get("l"), "close": c.get("c"), "volume": c.get("v", 0)})
    return rows


# ── CSV helpers ───────────────────────────────────────────────────────────────
def _csv_path(tf: str) -> Path:
    return DATA_DIR / TIMEFRAMES[tf]["csv"]


def _read_rows(tf: str) -> list:
    p = _csv_path(tf)
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_rows(tf: str, rows: list) -> None:
    with open(_csv_path(tf), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        w.writerows(rows)


def _last_ts_ms(rows: list):
    if not rows:
        return None
    last = rows[-1]["timestamp"]
    dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _age_secs(rows: list) -> float:
    ms = _last_ts_ms(rows)
    if ms is None:
        return float("inf")
    return (datetime.now(timezone.utc).timestamp() * 1000 - ms) / 1000.0


# ── public API ────────────────────────────────────────────────────────────────
def update(tf: str, force: bool = False) -> dict:
    """Refresh one timeframe's CSV. Fast path: if the cache is <10 min old, just read it. Otherwise fetch
    only the candles NEWER than the last stored one and append (never re-fetch stored history).
    Returns {timeframe, action, rows_total, rows_added, last_timestamp}."""
    if tf not in TIMEFRAMES:
        raise ValueError("unknown timeframe %r" % tf)
    rows = _read_rows(tf)

    if rows and not force and _age_secs(rows) < FRESH_SECS:
        return {"timeframe": tf, "action": "cached", "rows_total": len(rows),
                "rows_added": 0, "last_timestamp": rows[-1]["timestamp"]}

    key = _api_key()
    cfg = TIMEFRAMES[tf]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    if not rows:                                   # first run -> 2-year backfill
        frm = (datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)).strftime("%Y-%m-%d")
        to = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        new_rows = _candles_to_rows(_fetch_range(key, cfg["mult"], cfg["span"], frm, to))
        _write_rows(tf, new_rows)
        return {"timeframe": tf, "action": "backfill", "rows_total": len(new_rows),
                "rows_added": len(new_rows),
                "last_timestamp": new_rows[-1]["timestamp"] if new_rows else None}

    # incremental: fetch from just after the last stored candle
    frm_ms = _last_ts_ms(rows) + 1
    fetched = _candles_to_rows(_fetch_range(key, cfg["mult"], cfg["span"], frm_ms, now_ms))
    existing_last = _last_ts_ms(rows)
    add = [r for r in fetched
           if int(datetime.strptime(r["timestamp"], "%Y-%m-%dT%H:%M:%SZ")
                  .replace(tzinfo=timezone.utc).timestamp() * 1000) > existing_last]
    if add:
        with open(_csv_path(tf), "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_COLS).writerows(add)
        rows = rows + add
    return {"timeframe": tf, "action": "appended" if add else "up_to_date",
            "rows_total": len(rows), "rows_added": len(add),
            "last_timestamp": rows[-1]["timestamp"]}


def update_all(force: bool = False) -> dict:
    return {tf: update(tf, force=force) for tf in TIMEFRAMES}


def load(tf: str) -> list:
    """Read a timeframe's CSV as a list of row dicts with numeric OHLCV (for swing_levels etc.)."""
    out = []
    for r in _read_rows(tf):
        try:
            out.append({"timestamp": r["timestamp"], "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"]),
                        "volume": float(r["volume"]) if r.get("volume") not in (None, "") else 0.0})
        except (TypeError, ValueError):
            continue
    return out


if __name__ == "__main__":
    print("Merlin's Compass -- polygon_fetcher self-test (backfill all timeframes)")
    res = update_all(force=True)
    for tf, r in res.items():
        print("  %-6s %-9s rows=%-7d added=%-7d last=%s"
              % (tf, r["action"], r["rows_total"], r["rows_added"], r["last_timestamp"]))
