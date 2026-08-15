"""
compass/audusd_exit_backtest.py -- AUDUSD mean-reversion EXIT refinement (Gaius 028 #2 follow-up).
Reuses the EXACT baseline signal (1h BB20/2 + RSI 35/65, 5-min entry, stop 30 pips) -- only the EXIT changes.
Self-contained; analysis only, no Compass, nothing wired to live. Baseline for comparison: PF 1.40.

Variants:
  A  partial 50% at mid-band + remaining 50% on a 15-pip trailing stop, force-close 4h.
  B  baseline exit (full TP at mid-band, stop 30) but force-close 4h (vs 8h).
  C  baseline exit, force-close 6h.
  D  two-target: 50% at mid-band, 50% at the OPPOSITE band, stop 30, force-close 8h.
Convention (same as baseline): 1-pip spread/trade, pessimistic intrabar (stop before target), one position at a time.
"""
import numpy as np
import pandas as pd

try:
    from .meanrev_backtest import _prep
except ImportError:
    from meanrev_backtest import _prep

PIP = 10000.0
STOP = 0.0030          # 30 pips
TRAIL = 0.0015         # 15 pips
SPREAD_PIPS = 1.0
FC_BARS = {"A": 48, "B": 48, "C": 72, "D": 96}   # 4h / 4h / 6h / 8h (5-min bars)


def _signals(h):
    hc = h["close"].to_numpy(); hlo = h["lower"].to_numpy(); hup = h["upper"].to_numpy()
    hsma = h["sma20"].to_numpy(); hr = h["rsi"].to_numpy(); hep = h["epoch"].to_numpy()
    out = []
    for k in range(len(h)):
        if np.isnan(hlo[k]) or np.isnan(hr[k]):
            continue
        if hc[k] < hlo[k] and hr[k] < 35:
            out.append((hep[k], "LONG", hsma[k], hup[k]))      # (T, dir, midline, opposite band)
        elif hc[k] > hup[k] and hr[k] > 65:
            out.append((hep[k], "SHORT", hsma[k], hlo[k]))
    return out


def _simulate(variant, d, i, entry, stop, mid, opp, fo, fh, fl, fc, fep, n):
    """Return (pnl_price_weighted, exit_bar_index). Weighted over the fractions closed (sum=1)."""
    end = min(i + FC_BARS[variant], n - 1)
    realized = 0.0            # sum of frac * dir*(price-entry), in PRICE units
    remaining = 1.0
    half1 = False
    peak = entry              # LONG: running max high ; SHORT: running min low
    exit_i = end
    j = i
    while j <= end:
        if j > i and fep[j] - fep[j - 1] > 5400:      # data gap -> force out remaining at prev close
            p = fc[j - 1]
            realized += remaining * ((p - entry) if d == "LONG" else (entry - p)); remaining = 0.0; exit_i = j - 1
            break
        hi, lo = fh[j], fl[j]
        if not half1:
            # phase 1: whole position, 30-pip stop + mid-band target (pessimistic: stop first)
            if d == "LONG":
                if lo <= stop:
                    realized += remaining * (stop - entry); remaining = 0.0; exit_i = j; break
                if hi >= mid:
                    take = 1.0 if variant in ("B", "C") else 0.5
                    realized += take * (mid - entry); remaining -= take; exit_i = j
                    if remaining <= 0:
                        break
                    half1 = True; peak = hi
            else:
                if hi >= stop:
                    realized += remaining * (entry - stop); remaining = 0.0; exit_i = j; break
                if lo <= mid:
                    take = 1.0 if variant in ("B", "C") else 0.5
                    realized += take * (entry - mid); remaining -= take; exit_i = j
                    if remaining <= 0:
                        break
                    half1 = True; peak = lo
        else:
            # phase 2: the remaining 50%
            if variant == "A":                         # 15-pip trailing stop
                if d == "LONG":
                    peak = max(peak, hi); tstop = peak - TRAIL
                    if lo <= tstop:
                        realized += remaining * (tstop - entry); remaining = 0.0; exit_i = j; break
                else:
                    peak = min(peak, lo); tstop = peak + TRAIL
                    if hi >= tstop:
                        realized += remaining * (entry - tstop); remaining = 0.0; exit_i = j; break
            elif variant == "D":                       # opposite band target, keep 30-pip stop
                if d == "LONG":
                    if lo <= stop:
                        realized += remaining * (stop - entry); remaining = 0.0; exit_i = j; break
                    if hi >= opp:
                        realized += remaining * (opp - entry); remaining = 0.0; exit_i = j; break
                else:
                    if hi >= stop:
                        realized += remaining * (entry - stop); remaining = 0.0; exit_i = j; break
                    if lo <= opp:
                        realized += remaining * (entry - opp); remaining = 0.0; exit_i = j; break
        j += 1
    if remaining > 0:                                   # force-close leftover at the window end open
        p = fo[end]
        realized += remaining * ((p - entry) if d == "LONG" else (entry - p)); exit_i = end
    return realized, exit_i


def run(variant):
    h, f = _prep()
    fep = f["epoch"].to_numpy(); fo = f["open"].to_numpy(); fh = f["high"].to_numpy()
    fl = f["low"].to_numpy(); fc = f["close"].to_numpy(); fyr = f["year"].to_numpy()
    n = len(f)
    idx = {int(e): k for k, e in enumerate(fep)}
    trades = []; last_exit = 0
    for T, d, mid, opp in _signals(h):
        ep = T + 3600
        if ep < last_exit:
            continue
        i = idx.get(ep)
        if i is None or i + 1 >= n:
            continue
        entry = fo[i]
        stop = entry - STOP if d == "LONG" else entry + STOP
        realized, exit_i = _simulate(variant, d, i, entry, stop, mid, opp, fo, fh, fl, fc, fep, n)
        pnl = round(realized * PIP - SPREAD_PIPS, 1)
        trades.append({"year": int(fyr[i]), "pnl": pnl})
        last_exit = fep[exit_i] + 300
    return trades


def _stats(trades):
    n = len(trades)
    if not n:
        return "  (no trades)"
    pnl = [t["pnl"] for t in trades]
    w = [p for p in pnl if p > 0]; ls = [p for p in pnl if p <= 0]
    pf = (sum(w) / abs(sum(ls))) if ls else float("inf")
    return ("n=%-4d WR %4.1f%% PF %5.2f net %+8.1f pip  avgW +%.1f / avgL %.1f"
            % (n, 100.0 * len(w) / n, pf, sum(pnl),
               (sum(w) / len(w) if w else 0), (sum(ls) / len(ls) if ls else 0)))


def report():
    bar = "=" * 80
    print(bar); print("AUDUSD MEAN-REVERSION -- EXIT REFINEMENT (same signal, baseline PF 1.40)"); print(bar)
    desc = {"A": "A  partial 50% @ mid + 50% 15-pip trail, 4h FC",
            "B": "B  full TP @ mid, stop 30, 4h FC",
            "C": "C  full TP @ mid, stop 30, 6h FC",
            "D": "D  two-target: 50% @ mid + 50% @ opposite band, stop 30, 8h FC"}
    for v in ("A", "B", "C", "D"):
        trades = run(v)
        print("\n[%s]" % desc[v])
        print("  ALL   : " + _stats(trades))
        for y in (2024, 2025, 2026):
            print("   %d : %s" % (y, _stats([t for t in trades if t["year"] == y])))
    print("\n" + bar)
    print("BASELINE for reference: PF 1.40 | WR 51.5% | +1295 pip | avgW +22.2 / avgL -16.8 (396 trades, 8h FC)")
    print(bar)


if __name__ == "__main__":
    report()
