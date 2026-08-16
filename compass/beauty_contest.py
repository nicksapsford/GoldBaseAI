"""
compass/beauty_contest.py -- Mean-reversion "beauty contest" across all Polygon Currencies Starter
instruments (Archie brief 16 Aug 2026). Applies the PROVEN AUDUSD Bollinger+RSI mean-reversion signal
identically to USDCHF / EURUSD / GBPUSD / XAUEUR and ranks them. Analysis only -- no live, no Compass.

SIGNAL (identical to AUDUSD): 1h Bollinger(20, 2sigma) + RSI(14).
  LONG  : 1h close < lower band AND RSI < 30.   SHORT : 1h close > upper band AND RSI > 70.
  Entry : open of the first 5-min candle after the 1h close.  TP: middle band.  Stop: 30 pips/points.
  Force-close: 8 hours (96 x 5-min bars). One position at a time. Pessimistic intrabar (stop before TP).
VARIANT A: all sessions.  VARIANT B: entries only 13:30-20:00 UTC (US session).
Per-instrument scale: FX pip 0.0001 (6dp), XAUEUR gold-scale point 1.0 (2dp). Spread: FX 1.0 pip, XAUEUR 0.3 pt.
"""
import csv
import time
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

try:
    from . import polygon_fetcher as pf
except ImportError:
    import polygon_fetcher as pf
import data_feed_gold as feed   # reuse the exact RSI

BASE_URL = "https://api.polygon.io"
BACKFILL_DAYS = 730
FC_BARS = 96          # 8 hours of 5-min bars
US_START, US_END = 13 * 60 + 30, 20 * 60      # 13:30-20:00 UTC

# name, polygon ticker, pip/point size, stop (pips/pts), spread (pips/pts), price dp
INSTRUMENTS = [
    ("USDCHF", "C:USDCHF", 0.0001, 30.0, 1.0, 6),
    ("EURUSD", "C:EURUSD", 0.0001, 30.0, 1.0, 6),
    ("GBPUSD", "C:GBPUSD", 0.0001, 30.0, 1.0, 6),
    ("XAUEUR", "C:XAUEUR", 1.0,    30.0, 0.3, 2),
]


def _fetch(ticker, mult, span, frm, to):
    key = pf._api_key()
    url = "%s/v2/aggs/ticker/%s/range/%d/%s/%s/%s" % (BASE_URL, ticker, mult, span, frm, to)
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key}
    out, pages = [], 0
    while url and pages < 25:
        d = requests.get(url, params=params, timeout=60).json()
        out.extend(d.get("results") or [])
        url = d.get("next_url")
        if not url:
            break
        params = {"apiKey": key}; pages += 1; time.sleep(0.05)
    return out


def ensure(name, ticker):
    frm = (datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)).strftime("%Y-%m-%d")
    to = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    counts = {}
    for tf, mult, span in [("5min", 5, "minute"), ("1hour", 1, "hour")]:
        p = pf.DATA_DIR / ("bc_%s_%s.csv" % (name.lower(), tf))
        if p.exists():
            counts[tf] = sum(1 for _ in open(p, encoding="utf-8")) - 1
            continue
        rows, seen = [], set()
        for c in sorted(_fetch(ticker, mult, span, frm, to), key=lambda x: x["t"]):
            t = int(c["t"])
            if t in seen:
                continue
            seen.add(t)
            ts = datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append({"timestamp": ts, "open": c.get("o"), "high": c.get("h"),
                         "low": c.get("l"), "close": c.get("c"), "volume": c.get("v", 0)})
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
            w.writeheader(); w.writerows(rows)
        counts[tf] = len(rows)
    return counts


def _load(name, tf):
    p = pf.DATA_DIR / ("bc_%s_%s.csv" % (name.lower(), tf))
    df = pd.read_csv(p)
    df["dt"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.dropna(subset=["open", "high", "low", "close"]).sort_values("dt").reset_index(drop=True)


def _prep(name):
    h = _load(name, "1hour")
    h["rsi"] = feed._calc_rsi(h["close"])
    sma = h["close"].rolling(20).mean(); sd = h["close"].rolling(20).std()
    h["sma20"], h["lower"], h["upper"] = sma, sma - 2 * sd, sma + 2 * sd
    h["epoch"] = ((h["dt"] - pd.Timestamp("1970-01-01T00:00:00Z")) // pd.Timedelta(seconds=1)).astype("int64")
    f = _load(name, "5min")
    f["epoch"] = ((f["dt"] - pd.Timestamp("1970-01-01T00:00:00Z")) // pd.Timedelta(seconds=1)).astype("int64")
    f["year"] = f["dt"].dt.year
    f["hm"] = f["dt"].dt.hour * 60 + f["dt"].dt.minute
    return h, f


def run_variant(h, f, pip_size, stop_pips, spread_pips, us_only):
    PIP = 1.0 / pip_size
    stop_px = stop_pips * pip_size
    fep = f["epoch"].to_numpy(); fo = f["open"].to_numpy(); fh = f["high"].to_numpy()
    fl = f["low"].to_numpy(); fc = f["close"].to_numpy(); fyr = f["year"].to_numpy(); fhm = f["hm"].to_numpy()
    idx_of = {int(e): i for i, e in enumerate(fep)}
    hc = h["close"].to_numpy(); hlo = h["lower"].to_numpy(); hup = h["upper"].to_numpy()
    hsma = h["sma20"].to_numpy(); hr = h["rsi"].to_numpy(); hep = h["epoch"].to_numpy()
    signals = []
    for k in range(len(h)):
        if np.isnan(hlo[k]) or np.isnan(hr[k]):
            continue
        if hc[k] < hlo[k] and hr[k] < 30:
            signals.append((hep[k], "LONG", hsma[k]))
        elif hc[k] > hup[k] and hr[k] > 70:
            signals.append((hep[k], "SHORT", hsma[k]))
    trades = []
    last_exit_ep = 0
    for T, direction, tp in signals:
        entry_ep = T + 3600
        if entry_ep < last_exit_ep:
            continue
        i = idx_of.get(entry_ep)
        if i is None or i + 1 >= len(f):
            continue
        if us_only and not (US_START <= fhm[i] < US_END):     # session filter on the entry bar
            continue
        entry = fo[i]
        stop = entry - stop_px if direction == "LONG" else entry + stop_px
        exit_price, reason, mfe = None, None, 0.0
        end = min(i + FC_BARS, len(f) - 1)
        j = i
        for j in range(i, end + 1):
            if j > i and fep[j] - fep[j - 1] > 3600:
                exit_price, reason = fc[j - 1], "FORCE_CLOSE"; break
            fav = (fh[j] - entry) if direction == "LONG" else (entry - fl[j])
            if fav > mfe:
                mfe = fav
            if direction == "LONG":
                if fl[j] <= stop:  exit_price, reason = stop, "STOP"; break
                if fh[j] >= tp:    exit_price, reason = tp, "TP"; break
            else:
                if fh[j] >= stop:  exit_price, reason = stop, "STOP"; break
                if fl[j] <= tp:    exit_price, reason = tp, "TP"; break
        if exit_price is None:
            exit_price, reason, j = fo[end], "FORCE_CLOSE", end
        raw = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
        trades.append({"year": int(fyr[i]), "reason": reason,
                       "pnl": round(raw * PIP - spread_pips, 2), "mfe": round(mfe * PIP, 2)})
        last_exit_ep = fep[j] + 300
    return trades


def _pf(trades):
    if not trades:
        return None
    pnl = [t["pnl"] for t in trades]
    w = [p for p in pnl if p > 0]; ls = [p for p in pnl if p <= 0]
    return (sum(w) / abs(sum(ls))) if ls else float("inf")


def _stats(trades):
    n = len(trades)
    if not n:
        return "  (no trades)"
    pnl = [t["pnl"] for t in trades]
    w = [p for p in pnl if p > 0]; ls = [p for p in pnl if p <= 0]
    pf_ = _pf(trades)
    avg_w = (sum(w) / len(w)) if w else 0.0
    return ("n=%-4d WR %4.1f%% PF %5.2f net %+9.1f  avgW +%.1f/avgL %.1f (TP %d/ST %d/FC %d)"
            % (n, 100.0 * len(w) / n, pf_, sum(pnl), avg_w, (sum(ls) / len(ls) if ls else 0),
               sum(t["reason"] == "TP" for t in trades), sum(t["reason"] == "STOP" for t in trades),
               sum(t["reason"] == "FORCE_CLOSE" for t in trades)))


def _yearly(trades, years=(2024, 2025, 2026)):
    return {y: _pf([t for t in trades if t["year"] == y]) for y in years}


def report():
    import requests as _rq
    globals()["requests"] = _rq
    bar = "=" * 92
    print(bar); print("MEAN-REVERSION BEAUTY CONTEST -- Polygon Currencies Starter"); print(bar)
    print("Signal: 1h BB(20,2)+RSI | LONG close<lower & RSI<30 | SHORT close>upper & RSI>70 | TP=mid | 8h FC")
    print("Variant A = all sessions | Variant B = US session 13:30-20:00 UTC entries. Stop 30, one-at-a-time.\n")
    summary = []
    for name, ticker, pip, stop, spread, dp in INSTRUMENTS:
        try:
            counts = ensure(name, ticker)
        except Exception as exc:
            print("[%s] FETCH FAILED: %s\n" % (name, exc)); continue
        h, f = _prep(name)
        span = "%s .. %s" % (f["dt"].iloc[0].date(), f["dt"].iloc[-1].date()) if len(f) else "no data"
        print("-" * 92)
        print("%s (%s) | 5m %d, 1h %d candles | %s | pip=%.4f stop=%.0f spread=%.1f"
              % (name, ticker, counts.get("5min", 0), counts.get("1hour", 0), span, pip, stop, spread))
        tA = run_variant(h, f, pip, stop, spread, us_only=False)
        tB = run_variant(h, f, pip, stop, spread, us_only=True)
        print("  A all-sess : " + _stats(tA))
        yA = _yearly(tA)
        print("     yearly  : " + " | ".join("%d PF %s" % (y, ("%.2f" % v) if v is not None else "--") for y, v in yA.items()))
        print("  B US-sess  : " + _stats(tB))
        yB = _yearly(tB)
        print("     yearly  : " + " | ".join("%d PF %s" % (y, ("%.2f" % v) if v is not None else "--") for y, v in yB.items()))
        print()
        summary.append((name, tA, tB, yA, yB))
    print(bar); print("SUMMARY TABLE (PF / trades)"); print(bar)
    print("%-8s | %-22s | %-22s" % ("INSTR", "A all-sessions", "B US-session"))
    for name, tA, tB, yA, yB in summary:
        print("%-8s | PF %5s  n=%-4d | PF %5s  n=%-4d"
              % (name, ("%.2f" % _pf(tA)) if _pf(tA) else "--", len(tA),
                 ("%.2f" % _pf(tB)) if _pf(tB) else "--", len(tB)))
    print(bar)


if __name__ == "__main__":
    report()
