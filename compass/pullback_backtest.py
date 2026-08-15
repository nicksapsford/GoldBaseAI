"""
compass/pullback_backtest.py -- Gaius 027 H2 variant: London-break PULLBACK entry on GBPUSD.
Self-contained (not the SSL engine). Cached C:GBPUSD 5-min data. Analysis only -- no Compass, no live.

SPEC (implemented as written):
  * Asian range 00:00-07:55 UTC, filter 20-60 pips (same as the breakout test).
  * Wait for a break: first 5-min candle that CLOSES >=3 pips beyond the range high (LONG) / low (SHORT).
  * Then wait for a PULLBACK: enter when a later candle CLOSES within 2 pips of the broken boundary
    (|close - boundary| <= 2 pips).
  * Pullback must occur within 60 minutes of the break, else skip the day.
  * TP = 30 pips from entry, in the break direction.
  * Force-close 16:00 UTC. One trade/day. 1-pip spread. Pessimistic intrabar (stop before TP).

STOP -- the spec says "10 pips beyond the opposite side of the range (tight stop)". That phrase is ambiguous, so
both readings are reported:
  * "range" (literal 'opposite side of the range'): LONG stop = AL-10p ; SHORT stop = AH+10p   (wide, ~R+10p)
  * "tight" (matches the '(tight stop)' label, 10p from the entry boundary): LONG = AH-10p ; SHORT = AL+10p
"""
import pandas as pd

try:
    from . import gbpusd_data as gd
except ImportError:
    import gbpusd_data as gd

PIP = 10000.0
BREAK_BUFFER = 0.0003     # 3 pips
PULLBACK_TOL = 0.0002     # within 2 pips of the boundary
STOP_10 = 0.0010          # 10 pips
TP_PRICE = 0.0030         # 30 pips
SPREAD_PIPS = 1.0
PULLBACK_WINDOW_MIN = 60
ASIAN_END = 475; LONDON_START = 480; FC_HM = 960; MIN_ASIAN = 60


def _load():
    df = pd.DataFrame(gd.load("5min"))
    df["dt"] = pd.to_datetime(df["timestamp"], utc=True)
    df["hm"] = df["dt"].dt.hour * 60 + df["dt"].dt.minute
    df["date"] = df["dt"].dt.date
    df["year"] = df["dt"].dt.year
    return df


def run_variant(df, stop_mode):
    trades = []
    for date, day in df.groupby("date", sort=True):
        asian = day[day["hm"] <= ASIAN_END]
        if len(asian) < MIN_ASIAN:
            continue
        AH = asian["high"].max(); AL = asian["low"].min()
        R_pips = (AH - AL) * PIP
        if not (20.0 <= R_pips <= 60.0):
            continue

        w = day[(day["hm"] >= LONDON_START) & (day["hm"] < FC_HM)].sort_values("hm")
        hm = w["hm"].to_numpy(); o = w["open"].to_numpy(); h = w["high"].to_numpy()
        l = w["low"].to_numpy(); c = w["close"].to_numpy()
        yr = int(day["year"].iloc[0])

        # phase 1 -- first break
        bi = None; direction = None; boundary = None
        for i in range(len(w)):
            if c[i] > AH + BREAK_BUFFER:
                bi, direction, boundary = i, "LONG", AH; break
            if c[i] < AL - BREAK_BUFFER:
                bi, direction, boundary = i, "SHORT", AL; break
        if bi is None:
            continue

        # phase 2 -- pullback to the boundary within 60 min of the break
        entry_i = None
        for j in range(bi + 1, len(w)):
            if hm[j] > hm[bi] + PULLBACK_WINDOW_MIN:
                break
            if abs(c[j] - boundary) <= PULLBACK_TOL:
                entry_i = j; break
        if entry_i is None:
            continue

        entry = c[entry_i]
        if direction == "LONG":
            stop = (AL - STOP_10) if stop_mode == "range" else (AH - STOP_10)
            tp = entry + TP_PRICE
        else:
            stop = (AH + STOP_10) if stop_mode == "range" else (AL + STOP_10)
            tp = entry - TP_PRICE

        exit_price, reason, mfe = None, None, 0.0
        for k in range(entry_i + 1, len(w)):
            fav = (h[k] - entry) if direction == "LONG" else (entry - l[k])
            if fav > mfe:
                mfe = fav
            if direction == "LONG":
                if l[k] <= stop:  exit_price, reason = stop, "STOP"; break
                if h[k] >= tp:    exit_price, reason = tp, "TP"; break
            else:
                if h[k] >= stop:  exit_price, reason = stop, "STOP"; break
                if l[k] <= tp:    exit_price, reason = tp, "TP"; break
        if exit_price is None:
            fc = day[day["hm"] >= FC_HM].sort_values("hm")
            exit_price = float(fc["open"].iloc[0]) if len(fc) else float(c[-1])
            reason = "FORCE_CLOSE"

        raw = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
        trades.append({"year": yr, "direction": direction, "reason": reason,
                       "pnl": round(raw * PIP - SPREAD_PIPS, 1)})
    return trades


def _stats(trades):
    n = len(trades)
    if not n:
        return "  (no trades)"
    pnl = [t["pnl"] for t in trades]
    w = [p for p in pnl if p > 0]; ls = [p for p in pnl if p <= 0]
    pf = (sum(w) / abs(sum(ls))) if ls else float("inf")
    return ("n=%-4d  WR %4.1f%%  PF %5.2f  net %+8.1f pip  avgW +%.1f / avgL %.1f  (TP %d/STOP %d/FC %d)"
            % (n, 100.0 * len(w) / n, pf, sum(pnl),
               (sum(w) / len(w) if w else 0), (sum(ls) / len(ls) if ls else 0),
               sum(t["reason"] == "TP" for t in trades), sum(t["reason"] == "STOP" for t in trades),
               sum(t["reason"] == "FORCE_CLOSE" for t in trades)))


def run():
    df = _load()
    bar = "=" * 80
    print(bar); print("GAIUS 027 H2 -- GBPUSD LONDON-BREAK PULLBACK ENTRY (16:00 force-close)"); print(bar)
    print("Asian 00:00-07:55 | 20-60p | break>=3p then pullback within 2p of boundary <=60min | TP 30p | 1p spread\n")
    for mode, desc in [("tight", "STOP = 10p from entry boundary  (matches '(tight stop)': LONG AH-10p / SHORT AL+10p)"),
                       ("range", "STOP = 10p beyond opposite range side  (literal: LONG AL-10p / SHORT AH+10p, ~R+10p)")]:
        trades = run_variant(df, mode)
        print("[%s]" % desc)
        print("  ALL   : " + _stats(trades))
        for y in (2024, 2025, 2026):
            print("   %d : %s" % (y, _stats([t for t in trades if t["year"] == y])))
        print()
    print(bar)


if __name__ == "__main__":
    run()
