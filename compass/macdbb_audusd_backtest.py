"""
compass/macdbb_audusd_backtest.py -- 3-timeframe MACD-BB scalper on AUDUSD (C:AUDUSD).
Same MACD-BB signal as the Gold test (reuses _macd_colour / _resample / _ema from macdbb_backtest); AUDUSD data,
pip scaling (6-decimal), forward AND reverse (fade) direction. Self-contained; analysis only, no live.

FORWARD entry: all three MACD lines the same side of their BB midline (all green->LONG / all red->SHORT) + price on
the right side of the 5-min 20-EMA. Enter next 5-min open. Exit: TP / stop / 5-min colour flips back / 20:45 FC.
REVERSE: identical entry conditions, OPPOSITE trade direction (fade); the exit-signal stays keyed to the ORIGINAL
signal (hold the fade while the signal persists), TP/stop for the reversed side.
Stop/TP in pips (AUDUSD pip = 0.0001); 1-pip spread; one position at a time; pessimistic intrabar.
"""
import numpy as np
import pandas as pd

try:
    from . import audusd_data as ad
    from .macdbb_backtest import _macd_colour, _resample, _ema
except ImportError:
    import audusd_data as ad
    from macdbb_backtest import _macd_colour, _resample, _ema

PIP = 10000.0
SPREAD_PIPS = 1.0
FORCE_CLOSE_MIN = 20 * 60 + 45


def _flip(d):
    return "SHORT" if d == "LONG" else "LONG"


def run(stop_pips, tp_pips, ema_filter=True, reverse=False):
    five = pd.DataFrame(ad.load("5min"))
    five["dt"] = pd.to_datetime(five["timestamp"], utc=True)
    five = five.sort_values("dt").reset_index(drop=True)
    five["hm"] = five["dt"].dt.hour * 60 + five["dt"].dt.minute
    five["year"] = five["dt"].dt.year

    c5 = _macd_colour(five["close"], 20).to_numpy()
    ema20 = _ema(five["close"], 20).to_numpy()

    def htf(k, bb):
        htfdf = _resample(five, k)
        col = _macd_colour(htfdf["close"], bb).to_numpy()
        out = np.full(len(five), np.nan)
        for g in range(len(htfdf)):
            start = g * k + k
            if start >= len(five):
                break
            out[start: min((g + 1) * k + k, len(five))] = col[g]
        return out
    c15 = htf(3, 15); c30 = htf(6, 12)

    o = five["open"].to_numpy(); h = five["high"].to_numpy(); l = five["low"].to_numpy()
    cl = five["close"].to_numpy(); hm = five["hm"].to_numpy(); yr = five["year"].to_numpy()
    ep = ((five["dt"] - pd.Timestamp("1970-01-01T00:00:00Z")) // pd.Timedelta(seconds=1)).to_numpy()
    n = len(five)
    stop_px = stop_pips / PIP; tp_px = tp_pips / PIP

    trades = []
    in_trade = False; tdir = sig = None; entry = stop = tp = 0.0; entry_i = 0
    i = 1
    while i < n:
        if in_trade:
            if ep[i] - ep[i - 1] > 5400:
                _rec(trades, tdir, entry, cl[i - 1], entry_i, yr)
                in_trade = False; i += 1; continue
            hi, lo = h[i], l[i]
            xprice = None
            if hm[i] >= FORCE_CLOSE_MIN:
                xprice = o[i]
            elif tdir == "LONG":
                if lo <= stop:   xprice = stop
                elif hi >= tp:   xprice = tp
                elif c5[i - 1] == (-1 if sig == "LONG" else 1):  xprice = o[i]   # forward signal ended
            else:
                if hi >= stop:   xprice = stop
                elif lo <= tp:   xprice = tp
                elif c5[i - 1] == (-1 if sig == "LONG" else 1):  xprice = o[i]
            if xprice is not None:
                _rec(trades, tdir, entry, xprice, entry_i, yr)
                in_trade = False
            i += 1; continue

        j = i - 1
        if np.isnan(c5[j]) or np.isnan(c15[j]) or np.isnan(c30[j]):
            i += 1; continue
        if hm[i] >= FORCE_CLOSE_MIN:
            i += 1; continue
        green = c5[j] == 1 and c15[j] == 1 and c30[j] == 1
        red = c5[j] == -1 and c15[j] == -1 and c30[j] == -1
        if green and (not ema_filter or cl[j] > ema20[j]):
            sig = "LONG"
        elif red and (not ema_filter or cl[j] < ema20[j]):
            sig = "SHORT"
        else:
            i += 1; continue
        tdir = sig if not reverse else _flip(sig)
        entry = o[i]; entry_i = i
        stop = entry - stop_px if tdir == "LONG" else entry + stop_px
        tp = entry + tp_px if tdir == "LONG" else entry - tp_px
        in_trade = True; i += 1

    return trades


def _rec(trades, tdir, entry, xprice, entry_i, yr):
    raw = (xprice - entry) if tdir == "LONG" else (entry - xprice)
    trades.append({"year": int(yr[entry_i]), "pnl": round(raw * PIP - SPREAD_PIPS, 1)})


def _stats(trades):
    n = len(trades)
    if not n:
        return "  (none)"
    pnl = [t["pnl"] for t in trades]
    w = [p for p in pnl if p > 0]; ls = [p for p in pnl if p <= 0]
    pf = (sum(w) / abs(sum(ls))) if ls else float("inf")
    return ("n=%-5d WR %4.1f%% PF %5.2f net %+8.1f pip  avgW +%.1f / avgL %.1f"
            % (n, 100.0 * len(w) / n, pf, sum(pnl),
               (sum(w) / len(w) if w else 0), (sum(ls) / len(ls) if ls else 0)))


def report():
    bar = "=" * 84
    print(bar); print("3-TIMEFRAME MACD-BB SCALPER on AUDUSD (C:AUDUSD) -- forward + reverse"); print(bar)
    combos = [(15, 10), (20, 15), (25, 20), (30, 20)]
    for reverse in (False, True):
        print("\n" + ("### REVERSE (fade the signal)" if reverse else "### FORWARD (follow the signal)"))
        for stop, tp in combos:
            t = run(stop, tp, ema_filter=True, reverse=reverse)
            tag = " <- primary" if (stop, tp) == (20, 15) else ""
            print("  stop%d/TP%d%s" % (stop, tp, tag))
            print("    ALL  : " + _stats(t))
            for y in (2024, 2025, 2026):
                print("     %d: %s" % (y, _stats([x for x in t if x["year"] == y])))
    print("\n" + bar)
    print("AUDUSD mean-reversion BASELINE for reference: PF 1.40 (WR 51.5%, +1295 pip, positive all 3 yrs)")
    print(bar)


if __name__ == "__main__":
    report()
