"""
compass/outsidebar_backtest.py -- Outside Bar + Bollinger Band counter-trend reversal (Gold + AUDUSD).
4-hour outside bar at the outer 4h BB -> optional 1h midline-cross confirmation -> mean-revert to the far band.
Self-contained; analysis only, no live. (Cody brief 16 Aug 2026.)

Signal (4h candle, BB20/2 on 4h): OUTSIDE bar = high>prevHigh AND low<prevLow (engulfs prev range).
  BULLISH (long): low<=lower4  AND close in upper half  AND high<upper4 (second filter: room to target).
  BEARISH (short): high>=upper4 AND close in lower half AND low>lower4.
CONFIRM variant: after the 4h bar, wait for a 1h close to CROSS the 1h midline (LONG up / SHORT down) within 48h,
  else invalidate. Entry = 1h midline; stop = 1h opposite outer band; target = 1h entry-side outer band (~1:1).
NO-CONFIRM variant: enter at the 4h outside-bar close; stop/target = the 4h opposite/entry outer bands.
Interpretation notes (documented): CONFIRM uses the 1h BB (entry is the 1h midline -> ~1:1); NO-CONFIRM uses the 4h
BB (entry is the 4h close). Max hold 120x1h (~5 trading days) then force-close. Each signal independent (no 1-at-a-time).
"""
import numpy as np
import pandas as pd

try:
    from . import polygon_fetcher as pf
    from . import audusd_data as ad
except ImportError:
    import polygon_fetcher as pf
    import audusd_data as ad

MAXHOLD = 120


def _prep(inst):
    if inst == "GOLD":
        five = pd.DataFrame(pf.load("5min")); h1 = pd.DataFrame(pf.load("1hour"))
        PIP, SPREAD, unit = 1.0, 0.3, "pt"
    else:
        five = pd.DataFrame(ad.load("5min")); h1 = pd.DataFrame(ad.load("1hour"))
        PIP, SPREAD, unit = 10000.0, 1.0, "pip"
    e0 = pd.Timestamp("1970-01-01T00:00:00Z")
    for df in (five, h1):
        df["dt"] = pd.to_datetime(df["timestamp"], utc=True)
    five = five.sort_values("dt").reset_index(drop=True)
    h1 = h1.sort_values("dt").reset_index(drop=True)
    m = h1["close"].rolling(20).mean(); s = h1["close"].rolling(20).std()
    h1["mid"] = m; h1["up"] = m + 2 * s; h1["lo"] = m - 2 * s
    h1["ep"] = ((h1["dt"] - e0) // pd.Timedelta(seconds=1)).astype("int64")
    h1["year"] = h1["dt"].dt.year
    g = five.index // 48                                   # 48 x 5min = 4h
    h4 = pd.DataFrame({"high": five["high"].groupby(g).max(), "low": five["low"].groupby(g).min(),
                       "close": five["close"].groupby(g).last()}).reset_index(drop=True)
    last5 = five["dt"].groupby(g).last()
    h4["endep"] = (((last5 - e0) // pd.Timedelta(seconds=1)).values + 300)
    m4 = h4["close"].rolling(20).mean(); s4 = h4["close"].rolling(20).std()
    h4["up"] = m4 + 2 * s4; h4["lo"] = m4 - 2 * s4
    return h1, h4, PIP, SPREAD, unit


def run(inst):
    h1, h4, PIP, SPREAD, unit = _prep(inst)
    e1 = h1["ep"].to_numpy(); o1 = h1["open"].to_numpy(); hh1 = h1["high"].to_numpy()
    ll1 = h1["low"].to_numpy(); c1 = h1["close"].to_numpy(); mid1 = h1["mid"].to_numpy()
    up1 = h1["up"].to_numpy(); lo1 = h1["lo"].to_numpy(); yr1 = h1["year"].to_numpy(); n1 = len(h1)
    H = h4["high"].to_numpy(); L = h4["low"].to_numpy(); C = h4["close"].to_numpy()
    up4 = h4["up"].to_numpy(); lo4 = h4["lo"].to_numpy(); endep = h4["endep"].to_numpy(); n4 = len(h4)

    sigs = []
    for k in range(21, n4):
        if np.isnan(up4[k]) or np.isnan(lo4[k]):
            continue
        if not (H[k] > H[k - 1] and L[k] < L[k - 1]):     # outside bar
            continue
        midr = (H[k] + L[k]) / 2.0
        if L[k] <= lo4[k] and C[k] > midr and H[k] < up4[k]:
            sigs.append((endep[k], "LONG", C[k], up4[k], lo4[k]))
        elif H[k] >= up4[k] and C[k] < midr and L[k] > lo4[k]:
            sigs.append((endep[k], "SHORT", C[k], up4[k], lo4[k]))

    def manage(entry, stop, target, d, start):
        for m in range(start, min(start + MAXHOLD, n1)):
            if d == "LONG":
                if ll1[m] <= stop:   return stop
                if hh1[m] >= target: return target
            else:
                if hh1[m] >= stop:   return stop
                if ll1[m] <= target: return target
        return o1[min(start + MAXHOLD - 1, n1 - 1)]        # force-close

    confirm, noconf = [], []
    invalid = 0
    for Tk, d, c4, u4, l4 in sigs:
        start = int(np.searchsorted(e1, Tk, side="left"))
        if start < 1 or start >= n1:
            continue
        # --- NO-CONFIRM: enter at 4h close, 4h bands ---
        e = c4; st = l4 if d == "LONG" else u4; tg = u4 if d == "LONG" else l4
        xp = manage(e, st, tg, d, start)
        raw = (xp - e) if d == "LONG" else (e - xp)
        noconf.append({"pnl": round(raw * PIP - SPREAD, 2), "year": int(yr1[start])})
        # --- CONFIRM: 1h midline cross within 48h, enter at 1h midline, 1h bands ---
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
        xp = manage(e, st, tg, d, ej + 1)
        raw = (xp - e) if d == "LONG" else (e - xp)
        confirm.append({"pnl": round(raw * PIP - SPREAD, 2), "year": int(yr1[ej])})
    return {"sigs": len(sigs), "invalid": invalid, "confirm": confirm, "noconf": noconf, "unit": unit}


def _stats(trades, unit):
    n = len(trades)
    if not n:
        return "  (no trades)"
    pnl = [t["pnl"] for t in trades]
    w = [p for p in pnl if p > 0]; ls = [p for p in pnl if p <= 0]
    pf = (sum(w) / abs(sum(ls))) if ls else float("inf")
    return ("n=%-4d WR %4.1f%% PF %5.2f net %+8.1f %s  avgW +%.1f / avgL %.1f"
            % (n, 100.0 * len(w) / n, pf, sum(pnl), unit,
               (sum(w) / len(w) if w else 0), (sum(ls) / len(ls) if ls else 0)))


def report():
    bar = "=" * 82
    print(bar); print("OUTSIDE BAR + BOLLINGER BAND reversal -- Gold & AUDUSD (4h signal, 1h confirm)"); print(bar)
    for inst in ("GOLD", "AUDUSD"):
        r = run(inst); u = r["unit"]
        print("\n### %s  --  %d outside-bar-at-BB signals over 2yr (%.2f/week)"
              % (inst, r["sigs"], r["sigs"] / 104.0))
        print("  [WITH 1h confirmation]  (%d triggered / %d invalidated = %.0f%% confirm rate)"
              % (len(r["confirm"]), r["invalid"],
                 100.0 * len(r["confirm"]) / max(1, len(r["confirm"]) + r["invalid"])))
        print("    ALL  : " + _stats(r["confirm"], u))
        for y in (2024, 2025, 2026):
            print("     %d: %s" % (y, _stats([x for x in r["confirm"] if x["year"] == y], u)))
        print("  [WITHOUT confirmation -- enter on 4h close]")
        print("    ALL  : " + _stats(r["noconf"], u))
        for y in (2024, 2025, 2026):
            print("     %d: %s" % (y, _stats([x for x in r["noconf"] if x["year"] == y], u)))
    print("\n" + bar)
    print("Refs: GoldBase (trend) PF 1.15 | AUDUSD mean-rev baseline PF 1.40")
    print(bar)


if __name__ == "__main__":
    report()
