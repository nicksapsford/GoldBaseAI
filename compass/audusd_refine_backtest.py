"""
compass/audusd_refine_backtest.py -- AUDUSD mean-reversion refinements (RSI threshold + session filter).
Reuses the EXACT baseline (1h BB20/2 + RSI, 5-min entry, stop 30, TP=midline, 8h FC, 1p spread, one-at-a-time).
Changes ONE thing at a time. Baseline PF 1.40. Self-contained; analysis only, no live.
"""
import numpy as np
import pandas as pd

try:
    from .meanrev_backtest import _prep
except ImportError:
    from meanrev_backtest import _prep

PIP = 10000.0
STOP = 0.0030
SPREAD_PIPS = 1.0
FC_BARS = 96          # 8h


def run(rsi_long=35, rsi_short=65, sess=None):
    """sess = (start_min, end_min) UTC window for ENTRY, or None for all sessions. rsi_* = thresholds."""
    h, f = _prep()
    hc = h["close"].to_numpy(); hlo = h["lower"].to_numpy(); hup = h["upper"].to_numpy()
    hsma = h["sma20"].to_numpy(); hr = h["rsi"].to_numpy(); hep = h["epoch"].to_numpy()
    fep = f["epoch"].to_numpy(); fo = f["open"].to_numpy(); fh = f["high"].to_numpy()
    fl = f["low"].to_numpy(); fc = f["close"].to_numpy(); fyr = f["year"].to_numpy()
    fhm = (f["dt"].dt.hour * 60 + f["dt"].dt.minute).to_numpy()
    n = len(f)
    idx = {int(e): k for k, e in enumerate(fep)}

    signals = []
    for k in range(len(h)):
        if np.isnan(hlo[k]) or np.isnan(hr[k]):
            continue
        if hc[k] < hlo[k] and hr[k] < rsi_long:
            signals.append((hep[k], "LONG", hsma[k]))
        elif hc[k] > hup[k] and hr[k] > rsi_short:
            signals.append((hep[k], "SHORT", hsma[k]))

    trades = []; last_exit = 0
    for T, d, tp in signals:
        ep = T + 3600
        if ep < last_exit:
            continue
        i = idx.get(ep)
        if i is None or i + 1 >= n:
            continue
        if sess is not None and not (sess[0] <= fhm[i] < sess[1]):   # session filter on ENTRY bar
            continue
        entry = fo[i]
        stop = entry - STOP if d == "LONG" else entry + STOP
        xprice = None
        end = min(i + FC_BARS, n - 1)
        for j in range(i, end + 1):
            if j > i and fep[j] - fep[j - 1] > 3600:
                xprice = fc[j - 1]; jj = j - 1; break
            if d == "LONG":
                if fl[j] <= stop:  xprice = stop; jj = j; break
                if fh[j] >= tp:    xprice = tp; jj = j; break
            else:
                if fh[j] >= stop:  xprice = stop; jj = j; break
                if fl[j] <= tp:    xprice = tp; jj = j; break
        if xprice is None:
            xprice = fo[end]; jj = end
        raw = (xprice - entry) if d == "LONG" else (entry - xprice)
        trades.append({"year": int(fyr[i]), "pnl": round(raw * PIP - SPREAD_PIPS, 1)})
        last_exit = fep[jj] + 300
    return trades


def _stats(trades, days=None):
    n = len(trades)
    if not n:
        return "  (none)"
    pnl = [t["pnl"] for t in trades]
    w = [p for p in pnl if p > 0]; ls = [p for p in pnl if p <= 0]
    pf = (sum(w) / abs(sum(ls))) if ls else float("inf")
    tpd = ("  %.2f/day" % (n / days)) if days else ""
    return ("n=%-4d WR %4.1f%% PF %5.2f net %+8.1f pip%s" % (n, 100.0 * len(w) / n, pf, sum(pnl), tpd))


def report():
    h, _ = _prep()
    days = pd.to_datetime(h["timestamp"], utc=True).dt.date.nunique()
    bar = "=" * 78
    print(bar); print("AUDUSD MEAN-REVERSION REFINEMENTS -- baseline PF 1.40 (RSI 35/65, all sessions)"); print(bar)

    print("\n=== TEST 1 -- RSI THRESHOLD TIGHTENING ===")
    for rl, rs, lbl in [(35, 65, "35/65 baseline"), (30, 70, "30/70 tighter"),
                        (25, 75, "25/75 extreme"), (20, 80, "20/80 very rare")]:
        t = run(rsi_long=rl, rsi_short=rs)
        print("\n  [RSI %s]" % lbl)
        print("    ALL  : " + _stats(t, days))
        for y in (2024, 2025, 2026):
            print("     %d: %s" % (y, _stats([x for x in t if x["year"] == y])))

    print("\n\n=== TEST 2 -- SESSION FILTER (RSI 35/65 baseline) ===")
    sessions = [("All sessions", None), ("Asian 00:00-08:00", (0, 480)),
                ("London 08:00-16:00", (480, 960)), ("US 13:30-20:00", (810, 1200)),
                ("London+US overlap 13:30-16:00", (810, 960)), ("ex-Asian 08:00-24:00", (480, 1440))]
    for lbl, sess in sessions:
        t = run(sess=sess)
        print("\n  [%s]" % lbl)
        print("    ALL  : " + _stats(t, days))
        for y in (2024, 2025, 2026):
            print("     %d: %s" % (y, _stats([x for x in t if x["year"] == y])))
    print("\n" + bar)


if __name__ == "__main__":
    report()
