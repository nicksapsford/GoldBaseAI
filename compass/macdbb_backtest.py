"""
compass/macdbb_backtest.py -- 3-timeframe MACD-Bollinger-Band scalping signal on Gold (C:XAUUSD).
Self-contained (NOT the SSL/Lancelot engine -- a different signal). Uses the cached gold_5min.csv.
Analysis only -- no Compass, nothing wired to live. (Cody brief 15 Aug 2026.)

INDICATOR (identical MACD on all 3 TFs; BB period varies):
  MACD line = EMA12(close) - EMA26(close)   [signal EMA5 not needed -- the "colour" is the MACD line vs its own BB
  midline].  BB midline = EMA(period) of the MACD line; std = EMA-rolling std; colour = sign(MACD line - midline).
  GREEN = MACD line ABOVE its BB midline (bullish); RED = below.
  5-min TF: BB period 20 | 15-min TF: BB period 15 | 30-min TF: BB period 12 | std 1.5 (unused for colour but kept).

ENTRY: all three TFs same colour (all green->LONG / all red->SHORT) + (optional) 5-min price on the right side of the
20-EMA. Enter at the NEXT 5-min open. One position at a time.
EXIT priority: TP -> stop -> 5-min colour flips back across its midline (exit next open) -> 20:45 UTC force-close.
"""
import numpy as np
import pandas as pd

try:
    from . import polygon_fetcher as pf
except ImportError:
    import polygon_fetcher as pf

PIP = 1.0                # Gold "point" = $1 (1:1), matches GoldBase
SPREAD = 0.3             # Gold spread (points), same as strategy_gold
STAKE_GBP = 1.50         # £/pt for the £ column
FORCE_CLOSE_MIN = 20 * 60 + 45


def _ema(s, span):
    return s.ewm(span=span, adjust=False).mean()


def _macd_colour(close, bb_period):
    """MACD line (EMA12-EMA26) vs its own EMA-BB midline. Returns +1 (green) / -1 (red) per bar (NaN during warmup)."""
    macd = _ema(close, 12) - _ema(close, 26)
    mid = _ema(macd, bb_period)              # BB midline = EMA of the MACD line
    diff = macd - mid
    col = pd.Series(np.where(diff > 0, 1, -1), index=close.index, dtype=float)
    col[macd.isna() | mid.isna()] = np.nan
    # kill the warmup region (first slow+bb bars) so early bars aren't spuriously coloured
    col.iloc[: 26 + bb_period] = np.nan
    return col


def _resample(df5, k):
    """Derive k*5-min candles by grouping every k 5-min bars (3=15min, 6=30min). Perfect alignment with 5-min."""
    g = df5.index // k
    out = pd.DataFrame({
        "open":  df5["open"].groupby(g).first(),
        "high":  df5["high"].groupby(g).max(),
        "low":   df5["low"].groupby(g).min(),
        "close": df5["close"].groupby(g).last(),
        "endpos": df5.index.to_series().groupby(g).max().values,   # index of the LAST 5-min bar in each group
    }).reset_index(drop=True)
    return out


def run(stop_pts, tp_pts, ema_filter=True, verbose=False):
    five = pd.DataFrame(pf.load("5min"))
    five["dt"] = pd.to_datetime(five["timestamp"], utc=True)
    five = five.sort_values("dt").reset_index(drop=True)
    five["hm"] = five["dt"].dt.hour * 60 + five["dt"].dt.minute
    five["year"] = five["dt"].dt.year

    # 5-min colour + 20-EMA(price) filter
    c5 = _macd_colour(five["close"], 20).to_numpy()
    ema20 = _ema(five["close"], 20).to_numpy()

    # 15-min and 30-min colour, mapped back onto the 5-min grid by the LAST completed higher-TF bar (no lookahead)
    def htf_colour_on_5m(k, bb_period):
        htf = _resample(five, k)
        col = _macd_colour(htf["close"], bb_period).to_numpy()
        # a k-group's bar is only CLOSED after its last 5-min bar; map to 5-min bars AFTER that endpos
        endpos = htf["endpos"].to_numpy()
        out = np.full(len(five), np.nan)
        # for 5-min bar i, the applicable higher-TF colour is the last group whose endpos < i (already closed)
        # groups are contiguous: group g covers 5-min bars [g*k, g*k+k-1], closes at bar g*k+k-1.
        for g in range(len(htf)):
            start = g * k + k          # first 5-min bar for which group g is CLOSED
            if start >= len(five):
                break
            end = min((g + 1) * k + k, len(five))
            out[start:end] = col[g]
        return out
    c15 = htf_colour_on_5m(3, 15)
    c30 = htf_colour_on_5m(6, 12)

    o = five["open"].to_numpy(); h = five["high"].to_numpy(); l = five["low"].to_numpy()
    cl = five["close"].to_numpy(); hm = five["hm"].to_numpy(); yr = five["year"].to_numpy()
    ep = ((five["dt"] - pd.Timestamp("1970-01-01T00:00:00Z")) // pd.Timedelta(seconds=1)).to_numpy()
    n = len(five)

    trades = []
    in_trade = False; direction = None; entry = 0.0; entry_i = 0; stop = tp = 0.0; mfe = mae = 0.0
    i = 1
    while i < n:
        if in_trade:
            # gap safety: force out across a data gap
            if ep[i] - ep[i - 1] > 5400:
                _close(trades, direction, entry, cl[i - 1], "FORCE_CLOSE", entry_i, i - 1, ep, mfe, mae, yr)
                in_trade = False; i += 1; continue
            hi, lo = h[i], l[i]
            fav = (hi - entry) if direction == "LONG" else (entry - lo)
            adv = (entry - lo) if direction == "LONG" else (hi - entry)
            mfe = max(mfe, fav); mae = max(mae, adv)
            reason = xprice = None
            if hm[i] >= FORCE_CLOSE_MIN:
                reason, xprice = "FORCE_CLOSE", o[i]
            elif direction == "LONG":
                if lo <= stop:  reason, xprice = "STOP", stop
                elif hi >= tp:  reason, xprice = "TP", tp
                elif c5[i - 1] == -1:  reason, xprice = "SIGNAL", o[i]   # 5-min flipped red on the last closed bar
            else:
                if hi >= stop:  reason, xprice = "STOP", stop
                elif lo <= tp:  reason, xprice = "TP", tp
                elif c5[i - 1] == 1:   reason, xprice = "SIGNAL", o[i]
            if reason:
                _close(trades, direction, entry, xprice, reason, entry_i, i, ep, mfe, mae, yr)
                in_trade = False
            i += 1; continue

        # look for entry using the LAST CLOSED 5-min bar (i-1); enter at bar i's open
        j = i - 1
        if np.isnan(c5[j]) or np.isnan(c15[j]) or np.isnan(c30[j]):
            i += 1; continue
        if hm[i] >= FORCE_CLOSE_MIN or hm[i] < 0:
            i += 1; continue
        long_sig = c5[j] == 1 and c15[j] == 1 and c30[j] == 1
        short_sig = c5[j] == -1 and c15[j] == -1 and c30[j] == -1
        if long_sig and (not ema_filter or cl[j] > ema20[j]):
            direction = "LONG"
        elif short_sig and (not ema_filter or cl[j] < ema20[j]):
            direction = "SHORT"
        else:
            i += 1; continue
        entry = o[i]; entry_i = i
        stop = entry - stop_pts if direction == "LONG" else entry + stop_pts
        tp = entry + tp_pts if direction == "LONG" else entry - tp_pts
        mfe = mae = 0.0; in_trade = True; i += 1

    return trades


def _close(trades, direction, entry, xprice, reason, entry_i, exit_i, ep, mfe, mae, yr):
    raw = (xprice - entry) if direction == "LONG" else (entry - xprice)
    pts = raw - SPREAD
    dur_min = (ep[exit_i] - ep[entry_i]) / 60.0
    trades.append({"direction": direction, "reason": reason, "points": round(pts, 2),
                   "mfe": round(mfe, 2), "mae": round(mae, 2), "dur": dur_min,
                   "year": int(yr[entry_i]), "hour": None, "entry_i": entry_i})


def _stats(trades):
    n = len(trades)
    if not n:
        return None
    pts = [t["points"] for t in trades]
    w = [p for p in pts if p > 0]; ls = [p for p in pts if p <= 0]
    pf = (sum(w) / abs(sum(ls))) if ls else float("inf")
    return {"n": n, "wr": 100.0 * len(w) / n, "pf": pf, "net": sum(pts),
            "avgW": (sum(w) / len(w) if w else 0), "avgL": (sum(ls) / len(ls) if ls else 0),
            "dur": sum(t["dur"] for t in trades) / n,
            "tp": sum(t["reason"] == "TP" for t in trades), "stop": sum(t["reason"] == "STOP" for t in trades),
            "sig": sum(t["reason"] == "SIGNAL" for t in trades), "fc": sum(t["reason"] == "FORCE_CLOSE" for t in trades)}


def _line(s):
    return ("n=%-4d WR %4.1f%% PF %5.2f net %+8.1f pt (£%+8.0f) avgW+%.1f/avgL%.1f dur %4.1fmin "
            "| TP %d%%/STOP %d%%/SIG %d%%/FC %d%%"
            % (s["n"], s["wr"], s["pf"], s["net"], s["net"] * STAKE_GBP, s["avgW"], s["avgL"], s["dur"],
               round(100 * s["tp"] / s["n"]), round(100 * s["stop"] / s["n"]),
               round(100 * s["sig"] / s["n"]), round(100 * s["fc"] / s["n"])))


def report():
    bar = "=" * 92
    print(bar); print("3-TIMEFRAME MACD-BB SCALPER on GOLD (C:XAUUSD) -- baseline, no Compass"); print(bar)
    print("MACD 12/26 vs EMA-BB midline (5m BB20 / 15m BB15 / 30m BB12) | all-3-agree entry | 20:45 force-close\n")
    combos = [(20, 15), (20, 20), (25, 15), (25, 20), (25, 25), (30, 20), (30, 25)]
    best = None
    print("[STOP/TP SWEEP -- with EMA filter]")
    for stop, tp in combos:
        s = _stats(run(stop, tp, ema_filter=True))
        if s:
            tag = " <- primary" if (stop, tp) == (25, 20) else ""
            print("  stop%d/TP%d : %s%s" % (stop, tp, _line(s), tag))
            if best is None or s["pf"] > best[0]["pf"]:
                best = (s, stop, tp)
    print("\n[STOP/TP SWEEP -- WITHOUT EMA filter]")
    for stop, tp in combos:
        s = _stats(run(stop, tp, ema_filter=False))
        if s:
            print("  stop%d/TP%d : %s" % (stop, tp, _line(s)))

    # yearly + timing + signals/day for the BEST combo (with filter)
    bs, bstop, btp = best
    bt = run(bstop, btp, ema_filter=True)
    print("\n[BEST COMBO stop%d/TP%d -- YEARLY STABILITY]" % (bstop, btp))
    for y in (2024, 2025, 2026):
        ys = _stats([t for t in bt if t["year"] == y])
        if ys:
            print("  %d : %s" % (y, _line(ys)))
    # signals/day + timing
    five = pd.DataFrame(pf.load("5min")); five["dt"] = pd.to_datetime(five["timestamp"], utc=True)
    days = five["dt"].dt.date.nunique()
    print("\n[ACTIVITY]  %d trades over %d session-days = %.1f trades/day"
          % (bs["n"], days, bs["n"] / days))
    fivef = pd.DataFrame(pf.load("5min")); fivef["dt"] = pd.to_datetime(fivef["timestamp"], utc=True)
    hours = {}
    for t in bt:
        hh = int(fivef["dt"].iloc[t["entry_i"]].hour)
        hours[hh] = hours.get(hh, 0) + 1
    top = sorted(hours.items(), key=lambda kv: -kv[1])[:5]
    print("  busiest entry hours (UTC): " + ", ".join("%02d:00 (%d)" % (h, c) for h, c in top))
    print("\n[HEADLINE] best MACD-BB PF %.2f vs GoldBase baseline PF 1.15 -> %s"
          % (bs["pf"], "BEATS baseline" if bs["pf"] > 1.15 else ("matches ~baseline" if bs["pf"] >= 1.10 else "below baseline")))
    print(bar)


if __name__ == "__main__":
    report()
