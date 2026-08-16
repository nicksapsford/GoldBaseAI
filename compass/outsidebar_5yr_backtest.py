"""
compass/outsidebar_5yr_backtest.py -- Outside Bar + BB reversal on Gold, 5-YEAR validation (Aug 2021 -> Aug 2026).
Same signal as compass/outsidebar_backtest.py, CONFIRMATION-only. Fetches NATIVE 4h + 1h C:XAUUSD from Polygon
(lighter than 5yr of 5-min) and caches to compass/data/. Analysis only, no live.

Note: this uses Polygon's native 4h/1h aggregates (clock-aligned) vs the 2yr run which derived 4h from 5-min
(group x48). Bar alignment differs slightly, so 2024-26 numbers may not match the earlier run to the pip -- the
QUESTION is whether the edge holds across all 5 years.
"""
import csv
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

try:
    from . import polygon_fetcher as pf
except ImportError:
    import polygon_fetcher as pf

TICKER = "C:XAUUSD"
PIP, SPREAD = 1.0, 0.3          # Gold: point = $1, 0.3 spread
MAXHOLD = 120
START, END = "2021-08-01", "2026-08-16"


CHUNKS = [("2021-08-01", "2022-08-01"), ("2022-08-01", "2023-08-01"), ("2023-08-01", "2024-08-01"),
          ("2024-08-01", "2025-08-01"), ("2025-08-01", "2026-08-16")]   # yearly -- avoids the per-request span cap


def _fetch(mult, span, name):
    p = pf.DATA_DIR / name
    if p.exists():
        return
    key = pf._api_key()
    out, seen = [], set()
    for frm, to in CHUNKS:                        # fetch year-by-year (single 5yr request gets span-capped)
        url = "https://api.polygon.io/v2/aggs/ticker/%s/range/%d/%s/%s/%s" % (TICKER, mult, span, frm, to)
        params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key}
        pages = 0
        while url and pages < 10:
            d = requests.get(url, params=params, timeout=90).json()
            for c in (d.get("results") or []):
                if c["t"] not in seen:
                    seen.add(c["t"]); out.append(c)
            url = d.get("next_url")
            if not url:
                break
            params = {"apiKey": key}; pages += 1; time.sleep(0.05)
        time.sleep(0.05)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for c in sorted(out, key=lambda x: x["t"]):
            ts = datetime.fromtimestamp(c["t"] / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            w.writerow([ts, c.get("o"), c.get("h"), c.get("l"), c.get("c"), c.get("v", 0)])
    print("  fetched %s: %d candles" % (name, len(out)))


def _prep():
    _fetch(4, "hour", "gold_4h_5yr.csv")
    _fetch(1, "hour", "gold_1h_5yr.csv")
    e0 = pd.Timestamp("1970-01-01T00:00:00Z")
    h1 = pd.read_csv(pf.DATA_DIR / "gold_1h_5yr.csv"); h4 = pd.read_csv(pf.DATA_DIR / "gold_4h_5yr.csv")
    for df in (h1, h4):
        df["dt"] = pd.to_datetime(df["timestamp"], utc=True)
        df.sort_values("dt", inplace=True); df.reset_index(drop=True, inplace=True)
    m = h1["close"].rolling(20).mean(); s = h1["close"].rolling(20).std()
    h1["mid"] = m; h1["up"] = m + 2 * s; h1["lo"] = m - 2 * s
    h1["ep"] = ((h1["dt"] - e0) // pd.Timedelta(seconds=1)).astype("int64")
    h1["year"] = h1["dt"].dt.year
    m4 = h4["close"].rolling(20).mean(); s4 = h4["close"].rolling(20).std()
    h4["up"] = m4 + 2 * s4; h4["lo"] = m4 - 2 * s4
    h4["endep"] = ((h4["dt"] - e0) // pd.Timedelta(seconds=1)).astype("int64") + 4 * 3600
    return h1, h4


def run():
    h1, h4 = _prep()
    e1 = h1["ep"].to_numpy(); hh1 = h1["high"].to_numpy(); ll1 = h1["low"].to_numpy()
    o1 = h1["open"].to_numpy(); c1 = h1["close"].to_numpy(); mid1 = h1["mid"].to_numpy()
    up1 = h1["up"].to_numpy(); lo1 = h1["lo"].to_numpy(); yr1 = h1["year"].to_numpy(); n1 = len(h1)
    H = h4["high"].to_numpy(); L = h4["low"].to_numpy(); C = h4["close"].to_numpy()
    up4 = h4["up"].to_numpy(); lo4 = h4["lo"].to_numpy(); endep = h4["endep"].to_numpy(); n4 = len(h4)

    sigs = 0; invalid = 0; trades = []
    for k in range(21, n4):
        if np.isnan(up4[k]) or np.isnan(lo4[k]):
            continue
        if not (H[k] > H[k - 1] and L[k] < L[k - 1]):
            continue
        midr = (H[k] + L[k]) / 2.0
        if L[k] <= lo4[k] and C[k] > midr and H[k] < up4[k]:
            d = "LONG"
        elif H[k] >= up4[k] and C[k] < midr and L[k] > lo4[k]:
            d = "SHORT"
        else:
            continue
        sigs += 1
        start = int(np.searchsorted(e1, endep[k], side="left"))
        if start < 1 or start >= n1:
            continue
        ej = None
        for j in range(start, min(start + 48, n1)):
            if np.isnan(mid1[j]) or np.isnan(mid1[j - 1]):
                continue
            if d == "LONG" and c1[j] > mid1[j] and c1[j - 1] <= mid1[j - 1]:
                ej = j; break
            if d == "SHORT" and c1[j] < mid1[j] and c1[j - 1] >= mid1[j - 1]:
                ej = j; break
        if ej is None:
            invalid += 1; continue
        e = mid1[ej]; st = lo1[ej] if d == "LONG" else up1[ej]; tg = up1[ej] if d == "LONG" else lo1[ej]
        xp = None
        for m in range(ej + 1, min(ej + 1 + MAXHOLD, n1)):
            if d == "LONG":
                if ll1[m] <= st:  xp = st; break
                if hh1[m] >= tg:  xp = tg; break
            else:
                if hh1[m] >= st:  xp = st; break
                if ll1[m] <= tg:  xp = tg; break
        if xp is None:
            xp = o1[min(ej + MAXHOLD, n1 - 1)]
        raw = (xp - e) if d == "LONG" else (e - xp)
        trades.append({"pnl": round(raw * PIP - SPREAD, 2), "year": int(yr1[ej])})
    return sigs, invalid, trades


def _stats(trades):
    n = len(trades)
    if not n:
        return "  (no trades)"
    pnl = [t["pnl"] for t in trades]
    w = [p for p in pnl if p > 0]; ls = [p for p in pnl if p <= 0]
    pf = (sum(w) / abs(sum(ls))) if ls else float("inf")
    return ("n=%-3d WR %4.1f%% PF %5.2f net %+8.1f pt  avgW +%.1f / avgL %.1f"
            % (n, 100.0 * len(w) / n, pf, sum(pnl),
               (sum(w) / len(w) if w else 0), (sum(ls) / len(ls) if ls else 0)))


def report():
    bar = "=" * 80
    print(bar); print("OUTSIDE BAR + BB (Gold, confirmation-only) -- 5-YEAR VALIDATION 2021-2026"); print(bar)
    sigs, invalid, trades = run()
    print("\nsignals=%d | invalidated=%d | trades=%d (over ~5yr)\n" % (sigs, invalid, len(trades)))
    print("[ALL 5 YEARS] " + _stats(trades))
    print()
    for y in (2021, 2022, 2023, 2024, 2025, 2026):
        print("  %d: %s" % (y, _stats([x for x in trades if x["year"] == y])))
    print("\n" + bar)
    print("2-year run (derived-4h) reported PF 2.60 -- does it hold across all 5 years?")
    print(bar)


if __name__ == "__main__":
    report()
