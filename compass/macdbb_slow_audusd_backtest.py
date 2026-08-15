"""
compass/macdbb_slow_audusd_backtest.py -- SLOW 3-timeframe MACD-BB on AUDUSD (1h / 4h / daily).
Same MACD-BB logic as the fast test but on much slower timeframes to cut frequency (spread survival test).
Reuses _macd_colour / _ema from macdbb_backtest. Self-contained; analysis only, no live.

Timeframes: 1h (BB20, execution+exit) / 4h (BB15, from 1h grouped x4) / daily (BB12, audusd_daily.csv).
Entry: all-3 same colour (+1h 20-EMA filter) -> next 1h open. Exit: 1h colour flip / TP / stop / 24h FC.
Forward AND reverse (fade). Stop/TP in pips (pip=0.0001), 1-pip spread, one at a time, pessimistic intrabar.
"""
import numpy as np
import pandas as pd

try:
    from . import audusd_data as ad
    from .macdbb_backtest import _macd_colour, _ema
except ImportError:
    import audusd_data as ad
    from macdbb_backtest import _macd_colour, _ema

PIP = 10000.0
SPREAD_PIPS = 1.0
FC_BARS = 24            # 24 x 1h = 24h max
GAP_SECS = 6 * 3600     # >6h gap (weekend/holiday) -> force out


def _epoch(dtser):
    return ((dtser - pd.Timestamp("1970-01-01T00:00:00Z")) // pd.Timedelta(seconds=1)).to_numpy()


def _flip(d):
    return "SHORT" if d == "LONG" else "LONG"


def run(stop_pips, tp_pips, ema_filter=True, reverse=False):
    h = pd.DataFrame(ad.load("1hour"))
    h["dt"] = pd.to_datetime(h["timestamp"], utc=True)
    h = h.sort_values("dt").reset_index(drop=True)
    h["hm"] = 0; h["year"] = h["dt"].dt.year
    d = pd.DataFrame(ad.load("daily"))
    d["dt"] = pd.to_datetime(d["timestamp"], utc=True)
    d = d.sort_values("dt").reset_index(drop=True)

    # 1h colour (BB20) + 1h 20-EMA
    c1 = _macd_colour(h["close"], 20).to_numpy()
    ema20 = _ema(h["close"], 20).to_numpy()

    # 4h from 1h grouped x4 (BB15), mapped back to the 1h grid, no lookahead
    g = h.index // 4
    h4 = pd.DataFrame({"close": h["close"].groupby(g).last()}).reset_index(drop=True)
    c4raw = _macd_colour(h4["close"], 15).to_numpy()
    c4 = np.full(len(h), np.nan)
    for gi in range(len(h4)):
        start = gi * 4 + 4
        if start >= len(h):
            break
        c4[start: min((gi + 1) * 4 + 4, len(h))] = c4raw[gi]

    # daily colour (BB12), aligned to 1h by last CLOSED daily bar (no lookahead)
    cdraw = _macd_colour(d["close"], 12).to_numpy()
    hep = _epoch(h["dt"]); dep = _epoch(d["dt"])
    didx = np.searchsorted(dep, hep - 86400, side="right") - 1
    cd = np.where(didx >= 0, cdraw[np.clip(didx, 0, len(cdraw) - 1)], np.nan)

    o = h["open"].to_numpy(); hi = h["high"].to_numpy(); lo = h["low"].to_numpy()
    cl = h["close"].to_numpy(); yr = h["year"].to_numpy(); ep = hep
    n = len(h)
    stop_px = stop_pips / PIP; tp_px = tp_pips / PIP

    trades = []
    in_trade = False; tdir = sig = None; entry = stop = tp = 0.0; entry_i = 0; held = 0
    i = 1
    while i < n:
        if in_trade:
            if ep[i] - ep[i - 1] > GAP_SECS:
                _rec(trades, tdir, entry, cl[i - 1], entry_i, yr); in_trade = False; i += 1; continue
            held += 1
            xprice = None
            if held >= FC_BARS:
                xprice = o[i]
            elif tdir == "LONG":
                if lo[i] <= stop:  xprice = stop
                elif hi[i] >= tp:  xprice = tp
                elif c1[i - 1] == (-1 if sig == "LONG" else 1):  xprice = o[i]
            else:
                if hi[i] >= stop:  xprice = stop
                elif lo[i] <= tp:  xprice = tp
                elif c1[i - 1] == (-1 if sig == "LONG" else 1):  xprice = o[i]
            if xprice is not None:
                _rec(trades, tdir, entry, xprice, entry_i, yr); in_trade = False
            i += 1; continue

        j = i - 1
        if np.isnan(c1[j]) or np.isnan(c4[j]) or np.isnan(cd[j]):
            i += 1; continue
        green = c1[j] == 1 and c4[j] == 1 and cd[j] == 1
        red = c1[j] == -1 and c4[j] == -1 and cd[j] == -1
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
        in_trade = True; held = 0; i += 1

    return trades


def _rec(trades, tdir, entry, xprice, entry_i, yr):
    raw = (xprice - entry) if tdir == "LONG" else (entry - xprice)
    trades.append({"year": int(yr[entry_i]), "pnl": round(raw * PIP - SPREAD_PIPS, 1)})


def _stats(trades, days=None):
    n = len(trades)
    if not n:
        return "  (none)"
    pnl = [t["pnl"] for t in trades]
    w = [p for p in pnl if p > 0]; ls = [p for p in pnl if p <= 0]
    pf = (sum(w) / abs(sum(ls))) if ls else float("inf")
    tpd = ("  %.2f trades/day" % (n / days)) if days else ""
    return ("n=%-4d WR %4.1f%% PF %5.2f net %+8.1f pip  avgW +%.1f / avgL %.1f%s"
            % (n, 100.0 * len(w) / n, pf, sum(pnl),
               (sum(w) / len(w) if w else 0), (sum(ls) / len(ls) if ls else 0), tpd))


def report():
    bar = "=" * 84
    print(bar); print("SLOW 3-TIMEFRAME MACD-BB on AUDUSD (1h / 4h / daily) -- forward + reverse"); print(bar)
    h = pd.DataFrame(ad.load("1hour")); h["dt"] = pd.to_datetime(h["timestamp"], utc=True)
    days = h["dt"].dt.date.nunique()
    combos = [(40, 30), (50, 40), (30, 25)]
    for reverse in (False, True):
        print("\n" + ("### REVERSE (fade)" if reverse else "### FORWARD (follow)"))
        for stop, tp in combos:
            t = run(stop, tp, ema_filter=True, reverse=reverse)
            tag = " <- primary" if (stop, tp) == (40, 30) else ""
            print("  stop%d/TP%d%s" % (stop, tp, tag))
            print("    ALL  : " + _stats(t, days))
            for y in (2024, 2025, 2026):
                print("     %d: %s" % (y, _stats([x for x in t if x["year"] == y])))
    print("\n" + bar)
    print("AUDUSD mean-reversion BASELINE: PF 1.40 (WR 51.5%, +1295 pip, ~0.8 trades/day, positive all 3 yrs)")
    print("Fast MACD-BB (5/15/30m) for contrast: forward 0.69 / reverse 0.77 (~7 trades/day, spread-killed)")
    print(bar)


if __name__ == "__main__":
    report()
