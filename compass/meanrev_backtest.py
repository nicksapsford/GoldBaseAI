"""
compass/meanrev_backtest.py -- AUDUSD Bollinger mean-reversion baseline (Gaius 028 pipeline #2).
Self-contained (not the SSL engine). Cached C:AUDUSD data. Analysis only -- no Compass, no live.

SPEC (implemented exactly):
  * Bollinger Bands on 1-HOUR candles: SMA(20) +/- 2 std(20).  RSI(14) on 1-hour.
  * LONG  : 1h close < lower band AND 1h RSI < 35.
  * SHORT : 1h close > upper band AND 1h RSI > 65.
  * Entry : OPEN of the first 5-min candle after the 1h candle closes (start-time = signal-hour + 3600s). No lookahead.
  * Stop  : fixed pips (variants 20 / 30 / 40).
  * TP    : the middle band (SMA20) at signal time (a price level; ~20-40 pips depending on band width).
  * Force-close: 8 hours (96 x 5-min bars) after entry.
  * 24/5, no session filter. One position at a time. 1-pip spread. Pessimistic intrabar (stop before TP).
  * AUDUSD pip = 0.0001. Full price precision kept throughout (satisfies the 6dp requirement inherently).
"""
import numpy as np
import pandas as pd

try:
    from . import audusd_data as ad
except ImportError:
    import audusd_data as ad
import data_feed_gold as feed   # reuse the exact RSI

PIP = 10000.0
SPREAD_PIPS = 1.0
FC_BARS = 96          # 8 hours of 5-min bars
MGMT_MAX = FC_BARS


def _prep():
    h = pd.DataFrame(ad.load("1hour"))
    h["dt"] = pd.to_datetime(h["timestamp"], utc=True)
    h = h.sort_values("dt").reset_index(drop=True)
    h["rsi"] = feed._calc_rsi(h["close"])
    sma = h["close"].rolling(20).mean(); sd = h["close"].rolling(20).std()
    h["sma20"] = sma; h["lower"] = sma - 2 * sd; h["upper"] = sma + 2 * sd
    h["epoch"] = ((h["dt"] - pd.Timestamp("1970-01-01T00:00:00Z")) // pd.Timedelta(seconds=1)).astype("int64")

    f = pd.DataFrame(ad.load("5min"))
    f["dt"] = pd.to_datetime(f["timestamp"], utc=True)
    f = f.sort_values("dt").reset_index(drop=True)
    f["epoch"] = ((f["dt"] - pd.Timestamp("1970-01-01T00:00:00Z")) // pd.Timedelta(seconds=1)).astype("int64")
    f["year"] = f["dt"].dt.year
    return h, f


def run_variant(h, f, stop_pips):
    stop_px = stop_pips / PIP
    fep = f["epoch"].to_numpy(); fo = f["open"].to_numpy(); fh = f["high"].to_numpy()
    fl = f["low"].to_numpy(); fc = f["close"].to_numpy(); fyr = f["year"].to_numpy()
    idx_of = {int(e): i for i, e in enumerate(fep)}     # 5-min epoch -> row index

    # build the chronological signal list from the 1h frame
    hc = h["close"].to_numpy(); hlo = h["lower"].to_numpy(); hup = h["upper"].to_numpy()
    hsma = h["sma20"].to_numpy(); hr = h["rsi"].to_numpy(); hep = h["epoch"].to_numpy()
    signals = []
    for k in range(len(h)):
        if np.isnan(hlo[k]) or np.isnan(hr[k]):
            continue
        if hc[k] < hlo[k] and hr[k] < 35:
            signals.append((hep[k], "LONG", hsma[k]))
        elif hc[k] > hup[k] and hr[k] > 65:
            signals.append((hep[k], "SHORT", hsma[k]))

    trades = []
    last_exit_ep = 0
    for T, direction, tp in signals:
        entry_ep = T + 3600
        if entry_ep < last_exit_ep:            # one position at a time
            continue
        i = idx_of.get(entry_ep)
        if i is None or i + 1 >= len(f):
            continue
        entry = fo[i]
        stop = entry - stop_px if direction == "LONG" else entry + stop_px
        exit_price, reason, mfe = None, None, 0.0
        end = min(i + MGMT_MAX, len(f) - 1)
        for j in range(i, end + 1):
            if j > i and fep[j] - fep[j - 1] > 3600:       # data gap -> force out at prev close
                exit_price, reason = fc[j - 1], "FORCE_CLOSE"; break
            fav = (fh[j] - entry) if direction == "LONG" else (entry - fl[j])
            if fav > mfe:
                mfe = fav
            if direction == "LONG":
                if fl[j] <= stop:  exit_price, reason = stop, "STOP"; break
                if fh[j] >= tp:    exit_price, reason = tp, "TP"; break
            else:
                if fh[j] >= stop:  exit_price, reason = stop, "STOP"; break
                if fl[j] <= tp:    exit_price, reason = tp, "TP"; break
            exit_ep_bar = fep[j]
        if exit_price is None:                 # 8h force-close
            exit_price, reason = fo[end], "FORCE_CLOSE"
            exit_ep_bar = fep[end]
        else:
            exit_ep_bar = fep[j]
        raw = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
        trades.append({"year": int(fyr[i]), "direction": direction, "reason": reason,
                       "pnl": round(raw * PIP - SPREAD_PIPS, 1)})
        last_exit_ep = exit_ep_bar + 300
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
    h, f = _prep()
    bar = "=" * 80
    print(bar); print("AUDUSD BOLLINGER MEAN-REVERSION (1h BB20/2 + RSI, 5-min exec, TP=mid band)"); print(bar)
    print("LONG: 1h close<lower & RSI<35 | SHORT: 1h close>upper & RSI>65 | 8h FC | 1p spread | one-at-a-time\n")
    for stop_pips, label in [(30, "BASELINE  stop 30 pips"), (20, "TIGHTER   stop 20 pips"), (40, "WIDER     stop 40 pips")]:
        trades = run_variant(h, f, stop_pips)
        print("[%s]" % label)
        print("  ALL   : " + _stats(trades))
        for y in (2024, 2025, 2026):
            print("   %d : %s" % (y, _stats([t for t in trades if t["year"] == y])))
        print()
    print(bar)


if __name__ == "__main__":
    run()
