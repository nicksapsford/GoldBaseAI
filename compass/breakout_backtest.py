"""
compass/breakout_backtest.py -- Gaius Commission 027 Hypothesis 2: London-Open Breakout on GBPUSD.
================================================================================
Self-contained (does NOT use the SSL/Lancelot engine -- different signal). Uses the cached C:GBPUSD 5-min data.
Analysis only -- no Compass, nothing wired to live.

SPEC (implemented exactly as written):
  * Asian range = high/low of 00:00-07:55 UTC candles each day.
  * Range-size filter: trade only if Asian range is 20-60 pips.
  * Entry = first 5-min candle that CLOSES beyond the range high (LONG) / low (SHORT) by >= 3 pips buffer.
            Direction = whichever side breaks first. One trade per day.
  * Stop = Asian range midpoint.  TP = 0.75 x Asian range size (pips, from entry).
  * Force-close at the variant time (10:00 / 12:00 / 16:00 UTC) regardless.
  * Entry is scanned from 08:00 up to the force-close time (a trade cannot open after its own force-close);
    so shorter force-close windows naturally see fewer late-breaking days -- trade counts differ by variant.
  * Pip = 0.0001. A 1-pip round-trip spread is deducted per trade (realistic; the spec was spread-silent).
  * Intrabar = pessimistic (adverse extreme assumed first: stop checked before TP).
"""
import pandas as pd

try:
    from . import gbpusd_data as gd
except ImportError:
    import gbpusd_data as gd

PIP = 10000.0
BUFFER = 0.0003          # 3 pips
SPREAD_PIPS = 1.0        # round-trip cost deducted per trade
ASIAN_END = 475          # 07:55  (hm = hour*60+min)
LONDON_START = 480       # 08:00
MIN_ASIAN_CANDLES = 60


def _load():
    df = pd.DataFrame(gd.load("5min"))
    df["dt"] = pd.to_datetime(df["timestamp"], utc=True)
    df["hm"] = df["dt"].dt.hour * 60 + df["dt"].dt.minute
    df["date"] = df["dt"].dt.date
    df["year"] = df["dt"].dt.year
    return df


def run_variant(df, fc_hm):
    """Run the breakout with force-close at fc_hm (minutes-of-day). Returns list of trade dicts."""
    trades = []
    for date, day in df.groupby("date", sort=True):
        asian = day[day["hm"] <= ASIAN_END]
        if len(asian) < MIN_ASIAN_CANDLES:
            continue
        AH = asian["high"].max(); AL = asian["low"].min()
        R = AH - AL; R_pips = R * PIP
        if not (20.0 <= R_pips <= 60.0):
            continue
        mid = (AH + AL) / 2.0

        window = day[(day["hm"] >= LONDON_START) & (day["hm"] < fc_hm)].sort_values("hm")
        o = window["open"].to_numpy(); h = window["high"].to_numpy()
        l = window["low"].to_numpy(); c = window["close"].to_numpy()
        yr = int(day["year"].iloc[0])

        # find the first break (by close, with buffer)
        entry_i = None; direction = None
        for i in range(len(window)):
            if c[i] > AH + BUFFER:
                entry_i, direction = i, "LONG"; break
            if c[i] < AL - BUFFER:
                entry_i, direction = i, "SHORT"; break
        if entry_i is None:
            continue

        entry = c[entry_i]
        tp = entry + 0.75 * R if direction == "LONG" else entry - 0.75 * R
        stop = mid
        exit_price, reason, mfe = None, None, 0.0

        for j in range(entry_i + 1, len(window)):
            fav = (h[j] - entry) if direction == "LONG" else (entry - l[j])
            if fav > mfe:
                mfe = fav
            if direction == "LONG":
                if l[j] <= stop:   exit_price, reason = stop, "STOP"; break
                if h[j] >= tp:     exit_price, reason = tp, "TP"; break
            else:
                if h[j] >= stop:   exit_price, reason = stop, "STOP"; break
                if l[j] <= tp:     exit_price, reason = tp, "TP"; break

        if exit_price is None:      # force-close at the fc candle open (first bar at/after fc), else last close
            fc_bar = day[day["hm"] >= fc_hm].sort_values("hm")
            exit_price = float(fc_bar["open"].iloc[0]) if len(fc_bar) else float(window["close"].iloc[-1])
            reason = "FORCE_CLOSE"

        raw = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
        pnl = raw * PIP - SPREAD_PIPS
        trades.append({"date": str(date), "year": yr, "direction": direction, "reason": reason,
                       "pnl": round(pnl, 1), "mfe": round(mfe * PIP, 1), "R_pips": round(R_pips, 1)})
    return trades


def _stats(trades):
    n = len(trades)
    if not n:
        return "  (no trades)"
    pnl = [t["pnl"] for t in trades]
    w = [p for p in pnl if p > 0]; ls = [p for p in pnl if p <= 0]
    gp = sum(w); gl = abs(sum(ls))
    pf = (gp / gl) if gl else float("inf")
    return ("n=%-4d  WR %4.1f%%  PF %5.2f  net %+8.1f pip  avgW +%.1f / avgL %.1f  (TP %d/STOP %d/FC %d)"
            % (n, 100.0 * len(w) / n, pf, sum(pnl),
               (sum(w) / len(w) if w else 0), (sum(ls) / len(ls) if ls else 0),
               sum(t["reason"] == "TP" for t in trades), sum(t["reason"] == "STOP" for t in trades),
               sum(t["reason"] == "FORCE_CLOSE" for t in trades)))


def run():
    df = _load()
    bar = "=" * 78
    print(bar); print("GAIUS COMMISSION 027 H2 -- LONDON-OPEN BREAKOUT (GBPUSD, no Compass)"); print(bar)
    print("Asian range 00:00-07:55 | filter 20-60p | entry close>range+3p | stop=midpoint | TP=0.75xR")
    print("Spread 1 pip/trade | pessimistic intrabar | one trade/day\n")
    for fc_hm, label in [(600, "10:00 UTC"), (720, "12:00 UTC"), (960, "16:00 UTC")]:
        trades = run_variant(df, fc_hm)
        print("[FORCE-CLOSE %s]" % label)
        print("  ALL      : " + _stats(trades))
        for y in (2024, 2025, 2026):
            yt = [t for t in trades if t["year"] == y]
            print("   %d    : %s" % (y, _stats(yt)))
        print()
    print(bar)


if __name__ == "__main__":
    run()
