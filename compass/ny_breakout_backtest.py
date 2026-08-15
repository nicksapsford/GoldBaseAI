"""
compass/ny_breakout_backtest.py -- New York Open Breakout on GBPUSD (Gaius 027 H2 / Section 5B).
Same engine/data as the London breakout; NY open (13:30 UTC) breaking the LONDON-session range (08:00-13:25).
Self-contained; analysis only, no Compass, nothing wired to live.

BREAKOUT SPEC:
  * Range = high/low of 08:00-13:25 UTC candles (London session).  Filter: 30-80 pips.
  * Entry = first 5-min candle that CLOSES beyond the range by >=3 pips (LONG above / SHORT below), from 13:30 to
    the force-close time. Stop = range midpoint. TP = 0.75 x range (from entry). One trade/day. 1-pip spread.
  * Force-close variants: 15:00 / 16:00 / 17:00 UTC. Pessimistic intrabar (stop before TP).

PULLBACK SPEC (best from the London test -- literal wide stop):
  * After the break, enter when a candle closes within 2 pips of the broken boundary, within 60 min of the break.
  * Stop = 10 pips beyond the OPPOSITE side of the range (LONG: AL-10p / SHORT: AH+10p). TP = 30 pips. Force-close 16:00.
"""
import pandas as pd

try:
    from . import gbpusd_data as gd
except ImportError:
    import gbpusd_data as gd

PIP = 10000.0
BUFFER = 0.0003          # 3 pips
PULLBACK_TOL = 0.0002    # 2 pips
STOP_10 = 0.0010
TP_PULLBACK = 0.0030     # 30 pips
SPREAD_PIPS = 1.0
RANGE_START = 480        # 08:00
RANGE_END = 805          # 13:25
NY_START = 810           # 13:30
PULLBACK_WINDOW_MIN = 60
MIN_RANGE_CANDLES = 40


def _load():
    df = pd.DataFrame(gd.load("5min"))
    df["dt"] = pd.to_datetime(df["timestamp"], utc=True)
    df["hm"] = df["dt"].dt.hour * 60 + df["dt"].dt.minute
    df["date"] = df["dt"].dt.date
    df["year"] = df["dt"].dt.year
    return df


def _range(day):
    r = day[(day["hm"] >= RANGE_START) & (day["hm"] <= RANGE_END)]
    if len(r) < MIN_RANGE_CANDLES:
        return None
    AH = r["high"].max(); AL = r["low"].min()
    if not (30.0 <= (AH - AL) * PIP <= 80.0):
        return None
    return AH, AL


def run_breakout(df, fc_hm):
    trades = []
    for date, day in df.groupby("date", sort=True):
        rr = _range(day)
        if rr is None:
            continue
        AH, AL = rr; R = AH - AL; mid = (AH + AL) / 2.0
        w = day[(day["hm"] >= NY_START) & (day["hm"] < fc_hm)].sort_values("hm")
        o = w["open"].to_numpy(); h = w["high"].to_numpy(); l = w["low"].to_numpy(); c = w["close"].to_numpy()
        yr = int(day["year"].iloc[0])
        ei = direction = None
        for i in range(len(w)):
            if c[i] > AH + BUFFER:
                ei, direction = i, "LONG"; break
            if c[i] < AL - BUFFER:
                ei, direction = i, "SHORT"; break
        if ei is None:
            continue
        entry = c[ei]
        tp = entry + 0.75 * R if direction == "LONG" else entry - 0.75 * R
        stop = mid; xprice = reason = None
        for j in range(ei + 1, len(w)):
            if direction == "LONG":
                if l[j] <= stop: xprice, reason = stop, "STOP"; break
                if h[j] >= tp:   xprice, reason = tp, "TP"; break
            else:
                if h[j] >= stop: xprice, reason = stop, "STOP"; break
                if l[j] <= tp:   xprice, reason = tp, "TP"; break
        if xprice is None:
            fcbar = day[day["hm"] >= fc_hm].sort_values("hm")
            xprice = float(fcbar["open"].iloc[0]) if len(fcbar) else float(w["close"].iloc[-1])
            reason = "FORCE_CLOSE"
        raw = (xprice - entry) if direction == "LONG" else (entry - xprice)
        trades.append({"year": yr, "pnl": round(raw * PIP - SPREAD_PIPS, 1)})
    return trades


def run_pullback(df):
    trades = []
    for date, day in df.groupby("date", sort=True):
        rr = _range(day)
        if rr is None:
            continue
        AH, AL = rr
        w = day[(day["hm"] >= NY_START) & (day["hm"] < 960)].sort_values("hm")   # 13:30 -> 16:00
        hm = w["hm"].to_numpy(); h = w["high"].to_numpy(); l = w["low"].to_numpy(); c = w["close"].to_numpy()
        yr = int(day["year"].iloc[0])
        bi = direction = boundary = None
        for i in range(len(w)):
            if c[i] > AH + BUFFER: bi, direction, boundary = i, "LONG", AH; break
            if c[i] < AL - BUFFER: bi, direction, boundary = i, "SHORT", AL; break
        if bi is None:
            continue
        ei = None
        for j in range(bi + 1, len(w)):
            if hm[j] > hm[bi] + PULLBACK_WINDOW_MIN:
                break
            if abs(c[j] - boundary) <= PULLBACK_TOL:
                ei = j; break
        if ei is None:
            continue
        entry = c[ei]
        if direction == "LONG":
            stop = AL - STOP_10; tp = entry + TP_PULLBACK
        else:
            stop = AH + STOP_10; tp = entry - TP_PULLBACK
        xprice = reason = None
        for k in range(ei + 1, len(w)):
            if direction == "LONG":
                if l[k] <= stop: xprice, reason = stop, "STOP"; break
                if h[k] >= tp:   xprice, reason = tp, "TP"; break
            else:
                if h[k] >= stop: xprice, reason = stop, "STOP"; break
                if l[k] <= tp:   xprice, reason = tp, "TP"; break
        if xprice is None:
            fcbar = day[day["hm"] >= 960].sort_values("hm")
            xprice = float(fcbar["open"].iloc[0]) if len(fcbar) else float(w["close"].iloc[-1])
            reason = "FORCE_CLOSE"
        raw = (xprice - entry) if direction == "LONG" else (entry - xprice)
        trades.append({"year": yr, "pnl": round(raw * PIP - SPREAD_PIPS, 1)})
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
    df = _load()
    bar = "=" * 80
    print(bar); print("NEW YORK OPEN BREAKOUT on GBPUSD (London-session range 08:00-13:25, break >13:30)"); print(bar)
    print("Range 30-80p | close>range+3p entry | stop=midpoint | TP=0.75xR | 1p spread | one/day\n")
    for fc_hm, lbl in [(900, "15:00 UTC"), (960, "16:00 UTC"), (1020, "17:00 UTC")]:
        t = run_breakout(df, fc_hm)
        print("[BREAKOUT -- force-close %s]" % lbl)
        print("  ALL   : " + _stats(t))
        for y in (2024, 2025, 2026):
            print("   %d : %s" % (y, _stats([x for x in t if x["year"] == y])))
        print()
    tp = run_pullback(df)
    print("[PULLBACK -- break->retest, stop 10p beyond opposite side, TP 30p, 16:00 FC]")
    print("  ALL   : " + _stats(tp))
    for y in (2024, 2025, 2026):
        print("   %d : %s" % (y, _stats([x for x in tp if x["year"] == y])))
    print("\n" + bar)
    print("London-breakout reference (16:00 FC): PF 0.89 overall, 2025 PF 0.69 (the year that sank it)")
    print(bar)


if __name__ == "__main__":
    report()
