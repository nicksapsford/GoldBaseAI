"""
compass/us500_backtest.py -- Does the SSL/RSI/TMO Lancelot signal (the live USBase signal) have edge on US500?
Data: yfinance ES=F (S&P 500 futures). Reuses the EXACT signal (data_feed_gold.add_indicators + pre_checks_gold gate).
Real index points, 1:1. Stop 30 / TP 20 (USBase live, TP post-Commission-025), session 13:30-20:45 UTC, FC 20:45.
Analysis only, no live.

BT1 = 5-min signal (matches live USBase, ~60d of 5m). BT2 = 1-hour signal (~2yr of 1h). Fixed 30/20 stop/TP,
spread 0.4 pt, pessimistic intrabar. Entries only 13:30-20:00 UTC (US cash session, near-close buffer), FC 20:45.
"""
import csv
import numpy as np
import pandas as pd

try:
    from . import polygon_fetcher as pf
except ImportError:
    import polygon_fetcher as pf
import data_feed_gold as feed
import pre_checks_gold as pc

TICKER = "ES=F"
PIP, SPREAD = 1.0, 0.4
STOP, TP = 30.0, 20.0
SESSION_START = 13 * 60 + 30      # 13:30 UTC
NEAR_CLOSE, FORCE_CLOSE = 20 * 60, 20 * 60 + 45


def _fetch():
    import yfinance as yf, warnings
    warnings.filterwarnings("ignore")
    for interval, period, name in [("5m", "60d", "us500_5min.csv"), ("1h", "730d", "us500_1hour.csv")]:
        p = pf.DATA_DIR / name
        if p.exists():
            continue
        df = yf.download(TICKER, interval=interval, period=period, progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.reset_index()
        tcol = "Datetime" if "Datetime" in df.columns else "Date"
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            for _, r in df.iterrows():
                ts = pd.Timestamp(r[tcol]); ts = ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")
                w.writerow([ts, r["Open"], r["High"], r["Low"], r["Close"], r.get("Volume", 0)])
        print("  fetched %s: %d rows" % (name, len(df)))


def _load(name):
    df = pd.read_csv(pf.DATA_DIR / name)
    df["dt"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.dropna(subset=["open", "high", "low", "close"]).sort_values("dt").reset_index(drop=True)


def _enrich(df):
    e = feed.add_indicators(df.copy())
    e["ep"] = ((e["dt"] - pd.Timestamp("1970-01-01T00:00:00Z")) // pd.Timedelta(seconds=1)).astype("int64")
    e["hm"] = e["dt"].dt.hour * 60 + e["dt"].dt.minute
    e["year"] = e["dt"].dt.year
    return e


def _daily_from_1h(h1):
    h1 = h1.copy(); h1["date"] = h1["dt"].dt.date
    d = h1.groupby("date").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                               close=("close", "last"), volume=("volume", "sum")).reset_index()
    d["dt"] = pd.to_datetime(d["date"].astype(str) + " 00:00:00", utc=True)
    return _enrich(d)


def _run(fast, mid, daily, is_1h):
    fe = fast["ep"].to_numpy(); fo = fast["open"].to_numpy(); fh = fast["high"].to_numpy()
    fl = fast["low"].to_numpy(); fclose = fast["close"].to_numpy(); fhm = fast["hm"].to_numpy(); fyr = fast["year"].to_numpy()
    f_ssl = fast["ssl_bull"].to_numpy(); f_rsi = fast["rsi"].to_numpy(); f_tmo = fast["tmo_main"].to_numpy()
    me = mid["ep"].to_numpy(); m_ssl = mid["ssl_bull"].to_numpy(); m_rsi = mid["rsi"].to_numpy()
    de = daily["ep"].to_numpy(); d_ssl = daily["ssl_bull"].to_numpy()
    n = len(fast)
    trades = []; in_trade = False; direction = None; entry = stop = tp = 0.0
    for i in range(1, n):
        if in_trade:
            if fe[i] - fe[i - 1] > 3 * 3600:
                _rec(trades, direction, entry, fclose[i - 1], fyr, i); in_trade = False; continue
            if fhm[i] >= FORCE_CLOSE or fhm[i] < SESSION_START:
                _rec(trades, direction, entry, fo[i], fyr, i); in_trade = False; continue
            if direction == "LONG":
                if fl[i] <= stop:  _rec(trades, direction, entry, stop, fyr, i); in_trade = False; continue
                if fh[i] >= tp:    _rec(trades, direction, entry, tp, fyr, i); in_trade = False; continue
            else:
                if fh[i] >= stop:  _rec(trades, direction, entry, stop, fyr, i); in_trade = False; continue
                if fl[i] <= tp:    _rec(trades, direction, entry, tp, fyr, i); in_trade = False; continue
            continue
        j = i - 1
        if fhm[i] < SESSION_START or fhm[i] >= NEAR_CLOSE:      # US cash-session entries only
            continue
        hidx = int(np.searchsorted(me, fe[i] - (0 if is_1h else 3600), side="right")) - 1
        didx = int(np.searchsorted(de, fe[i] - 86400, side="right")) - 1
        if hidx < 20 or didx < 12:
            continue
        if np.isnan(f_ssl[j]) or np.isnan(m_ssl[hidx]) or np.isnan(d_ssl[didx]):
            continue
        if np.isnan(f_rsi[j]) or np.isnan(f_tmo[j]) or np.isnan(m_rsi[hidx]):
            continue
        d, h, m = bool(d_ssl[didx]), bool(m_ssl[hidx]), bool(f_ssl[j])
        if not (d == h == m):
            continue
        direction0 = "LONG" if d else "SHORT"
        if direction0 == "SHORT":
            recent = d_ssl[max(0, didx - 2): didx + 1]
            if d_ssl[didx] or len(recent) < 3 or bool(np.any(recent)):
                continue
        bar1h = {"ssl_bull": h, "rsi": m_rsi[hidx]}
        barf = {"ssl_bull": m, "rsi": f_rsi[j], "tmo_main": f_tmo[j], "open": fo[j], "close": fclose[j]}
        if not pc.check_ssl_agreement(bar1h, barf, direction0)["passed"]:      continue
        if not pc.check_1h_rsi_confirms(bar1h, direction0, False)["passed"]:   continue
        if not pc.check_5m_tmo_momentum(bar1h, barf)["passed"]:               continue
        if not pc.check_choppy_market(bar1h, barf)["passed"]:                 continue
        if not pc.check_candle_confirmed(bar1h, barf)["passed"]:              continue
        direction = direction0; entry = fo[i]
        stop = entry - STOP if direction == "LONG" else entry + STOP
        tp = entry + TP if direction == "LONG" else entry - TP
        in_trade = True
    return trades


def _rec(trades, d, entry, xprice, fyr, i):
    raw = (xprice - entry) if d == "LONG" else (entry - xprice)
    trades.append({"pnl": round(raw * PIP - SPREAD, 3), "year": int(fyr[i])})


def _stats(trades):
    n = len(trades)
    if not n:
        return "  (no trades)"
    pnl = [t["pnl"] for t in trades]
    w = [p for p in pnl if p > 0]; ls = [p for p in pnl if p <= 0]
    pf = (sum(w) / abs(sum(ls))) if ls else float("inf")
    return ("n=%-4d WR %4.1f%% PF %5.2f net %+8.1f pt  avgW +%.1f / avgL %.1f"
            % (n, 100.0 * len(w) / n, pf, sum(pnl),
               (sum(w) / len(w) if w else 0), (sum(ls) / len(ls) if ls else 0)))


def report():
    _fetch()
    five = _enrich(_load("us500_5min.csv")); h1 = _enrich(_load("us500_1hour.csv"))
    daily = _daily_from_1h(h1)
    bar = "=" * 78
    print(bar); print("US500 (ES=F) -- SSL/RSI/TMO Lancelot signal (the live USBase signal)"); print(bar)
    print("5m span: %s..%s | 1h span: %s..%s\n"
          % (five["dt"].iloc[0].date(), five["dt"].iloc[-1].date(),
             h1["dt"].iloc[0].date(), h1["dt"].iloc[-1].date()))
    t1 = _run(five, h1, daily, is_1h=False)
    print("[BACKTEST 1 -- 5-min signal (matches live USBase, ~60 days)]")
    print("  ALL  : " + _stats(t1))
    for y in sorted({t["year"] for t in t1}):
        print("   %d: %s" % (y, _stats([x for x in t1 if x["year"] == y])))
    t2 = _run(h1, h1, daily, is_1h=True)
    print("\n[BACKTEST 2 -- 1-hour signal (~2 years)]")
    print("  ALL  : " + _stats(t2))
    for y in (2024, 2025, 2026):
        print("   %d: %s" % (y, _stats([x for x in t2 if x["year"] == y])))
    print("\n" + bar)
    print("KEY: PF>1.10 consistently = keep USBase live | PF<1.00 consistently = demote (like Oil)")
    print(bar)


if __name__ == "__main__":
    report()
