"""
compass/gbpusd_reverse_backtest.py -- REVERSED (fade) GBPUSD breakout/pullback variants.
Same entries as the original breakout/pullback tests; DIRECTION reversed. Self-contained; analysis only.

"Reverse, same stops/TPs" is implemented as the TRUE MIRROR: same entry price, opposite direction, and the stop and
TP LEVELS swap roles (original TP-level becomes the stop; original stop/midpoint becomes the TP). A literal
"stop = midpoint" on a reversed trade would sit on the wrong side (instant stop-out), so role-swap is the only
sensible reading -- and it is the exact mirror that inverts the P&L path (gross PF ~ 1/original, minus the ever-present
1-pip spread). Pessimistic intrabar retained (each variant checks its own stop before its TP).
"""
import pandas as pd

try:
    from . import gbpusd_data as gd
except ImportError:
    import gbpusd_data as gd

PIP = 10000.0
BUFFER = 0.0003; PULLBACK_TOL = 0.0002; STOP_10 = 0.0010; TP_PULLBACK = 0.0030
SPREAD_PIPS = 1.0; PULLBACK_WINDOW_MIN = 60; MIN_RANGE_CANDLES = 40


def _load():
    df = pd.DataFrame(gd.load("5min"))
    df["dt"] = pd.to_datetime(df["timestamp"], utc=True)
    df["hm"] = df["dt"].dt.hour * 60 + df["dt"].dt.minute
    df["date"] = df["dt"].dt.date
    df["year"] = df["dt"].dt.year
    return df


def _rng(day, rs, re, rmin, rmax):
    r = day[(day["hm"] >= rs) & (day["hm"] <= re)]
    if len(r) < MIN_RANGE_CANDLES:
        return None
    AH = r["high"].max(); AL = r["low"].min()
    if not (rmin <= (AH - AL) * PIP <= rmax):
        return None
    return AH, AL


def _manage(w_o, w_h, w_l, ei, direction, stop, tp, day, fc_hm):
    """Simulate from ei+1; return exit price + reason. Pessimistic: stop before TP."""
    for j in range(ei + 1, len(w_h)):
        if direction == "LONG":
            if w_l[j] <= stop: return stop, "STOP"
            if w_h[j] >= tp:   return tp, "TP"
        else:
            if w_h[j] >= stop: return stop, "STOP"
            if w_l[j] <= tp:   return tp, "TP"
    fc = day[day["hm"] >= fc_hm].sort_values("hm")
    return (float(fc["open"].iloc[0]) if len(fc) else float(w_o[-1])), "FORCE_CLOSE"


def breakout(df, rs, re, es, fc_hm, rmin, rmax, reverse):
    trades = []
    for date, day in df.groupby("date", sort=True):
        rr = _rng(day, rs, re, rmin, rmax)
        if rr is None:
            continue
        AH, AL = rr; R = AH - AL; mid = (AH + AL) / 2.0
        w = day[(day["hm"] >= es) & (day["hm"] < fc_hm)].sort_values("hm")
        o = w["open"].to_numpy(); h = w["high"].to_numpy(); l = w["low"].to_numpy(); c = w["close"].to_numpy()
        ei = d0 = None
        for i in range(len(w)):
            if c[i] > AH + BUFFER: ei, d0 = i, "LONG"; break
            if c[i] < AL - BUFFER: ei, d0 = i, "SHORT"; break
        if ei is None:
            continue
        entry = c[i]
        stop0 = mid; tp0 = entry + 0.75 * R if d0 == "LONG" else entry - 0.75 * R
        d, stop, tp = (d0, stop0, tp0) if not reverse else (_flip(d0), tp0, stop0)   # MIRROR: swap stop<->tp
        xp, _r = _manage(o, h, l, ei, d, stop, tp, day, fc_hm)
        raw = (xp - entry) if d == "LONG" else (entry - xp)
        trades.append({"year": int(day["year"].iloc[0]), "pnl": round(raw * PIP - SPREAD_PIPS, 1)})
    return trades


def pullback(df, rs, re, es, fc_hm, rmin, rmax, reverse):
    trades = []
    for date, day in df.groupby("date", sort=True):
        rr = _rng(day, rs, re, rmin, rmax)
        if rr is None:
            continue
        AH, AL = rr
        w = day[(day["hm"] >= es) & (day["hm"] < fc_hm)].sort_values("hm")
        hm = w["hm"].to_numpy(); o = w["open"].to_numpy(); h = w["high"].to_numpy()
        l = w["low"].to_numpy(); c = w["close"].to_numpy()
        bi = d0 = bnd = None
        for i in range(len(w)):
            if c[i] > AH + BUFFER: bi, d0, bnd = i, "LONG", AH; break
            if c[i] < AL - BUFFER: bi, d0, bnd = i, "SHORT", AL; break
        if bi is None:
            continue
        ei = None
        for j in range(bi + 1, len(w)):
            if hm[j] > hm[bi] + PULLBACK_WINDOW_MIN:
                break
            if abs(c[j] - bnd) <= PULLBACK_TOL:
                ei = j; break
        if ei is None:
            continue
        entry = c[ei]
        stop0 = (AL - STOP_10) if d0 == "LONG" else (AH + STOP_10)
        tp0 = entry + TP_PULLBACK if d0 == "LONG" else entry - TP_PULLBACK
        d, stop, tp = (d0, stop0, tp0) if not reverse else (_flip(d0), tp0, stop0)
        xp, _r = _manage(o, h, l, ei, d, stop, tp, day, fc_hm)
        raw = (xp - entry) if d == "LONG" else (entry - xp)
        trades.append({"year": int(day["year"].iloc[0]), "pnl": round(raw * PIP - SPREAD_PIPS, 1)})
    return trades


def _flip(d):
    return "SHORT" if d == "LONG" else "LONG"


def _stats(trades):
    n = len(trades)
    if not n:
        return "  (none)"
    pnl = [t["pnl"] for t in trades]
    w = [p for p in pnl if p > 0]; ls = [p for p in pnl if p <= 0]
    pf = (sum(w) / abs(sum(ls))) if ls else float("inf")
    return ("n=%-4d WR %4.1f%% PF %5.2f net %+8.1f pip  avgW +%.1f / avgL %.1f"
            % (n, 100.0 * len(w) / n, pf, sum(pnl),
               (sum(w) / len(w) if w else 0), (sum(ls) / len(ls) if ls else 0)))


# range params: Asian (London-breakout) vs London (NY-breakout)
ASIAN = dict(rs=0, re=475, es=480, rmin=20, rmax=60)
LONDON = dict(rs=480, re=805, es=810, rmin=30, rmax=80)


def report():
    df = _load()
    bar = "=" * 82
    print(bar); print("GBPUSD BREAKOUT/PULLBACK -- DIRECTION REVERSED (fade) -- true mirror, 1p spread"); print(bar)
    variants = [
        ("London Breakout 16:00", "orig PF 0.89", lambda: breakout(df, fc_hm=960, reverse=True, **ASIAN)),
        ("London Pullback 16:00", "orig PF 1.04", lambda: pullback(df, fc_hm=960, reverse=True, **ASIAN)),
        ("NY Breakout 15:00  *",  "orig PF 0.56", lambda: breakout(df, fc_hm=900, reverse=True, **LONDON)),
        ("NY Breakout 16:00",     "orig PF 0.87", lambda: breakout(df, fc_hm=960, reverse=True, **LONDON)),
        ("NY Pullback 16:00",     "orig PF 0.89", lambda: pullback(df, fc_hm=960, reverse=True, **LONDON)),
    ]
    for name, ref, fn in variants:
        t = fn()
        print("\n[%s REVERSED]  (%s)" % (name, ref))
        print("  ALL   : " + _stats(t))
        for y in (2024, 2025, 2026):
            print("   %d : %s" % (y, _stats([x for x in t if x["year"] == y])))
    print("\n" + bar)
    print("* NY Breakout 15:00 original PF 0.56 -> theory ~1/0.56 = 1.79 gross (less 1p spread)")
    print(bar)


if __name__ == "__main__":
    report()
