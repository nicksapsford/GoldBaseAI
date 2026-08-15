"""
compass/silver_backtest.py -- Silver (C:XAGUSD) Session-2 baseline backtest.
Same engine + Lancelot signal logic as Gold (SSL/RSI/TMO gate, two-speed trail, force-close), driven with the
Silver strategy module (cent-scaled: 40c stop / 25c TP) and Silver data. No Compass. Analysis only.

Note on units: the engine works in the instrument's native price ($). Silver's "point" = $0.01 (a cent), so
$ P&L is reported x100 (cents) to match the 40c/25c spec and be directly comparable to Gold's point numbers.
"""
import logging
logging.disable(logging.CRITICAL)

try:
    from . import silver_data, strategy_silver
    from .backtest_engine import run_baseline
    from .backtest_report import report, _pf
except ImportError:
    import silver_data, strategy_silver
    from backtest_engine import run_baseline
    from backtest_report import report, _pf

SILVER_STAKE_PER_CENT = 0.50   # £20 risk / 40c stop = £50/$ = £0.50 per cent (context only; PF/WR size-independent)


def _to_cents(trades):
    for t in trades:
        for k in ("points", "mfe", "mae", "upside_pts"):
            if t.get(k) is not None:
                t[k] = round(t[k] * 100, 2)
    return trades


def run():
    silver_data.ensure_data()

    # --- Gold regression: engine unchanged still gives 849 ---
    gold = run_baseline(verbose=False)
    print(">> Gold regression (engine unchanged): %d trades, PF %.2f  (expect 849 / 1.15)"
          % (len(gold), _pf([t["points"] for t in gold if t["points"] > 0],
                            [t["points"] for t in gold if t["points"] <= 0])))

    # --- Silver baseline (primary: 2c spread) ---
    strategy_silver.SPREAD_POINTS = 0.02
    trades = _to_cents(run_baseline(verbose=False, strat_mod=strategy_silver, loader=silver_data.load))
    report(trades, stake=SILVER_STAKE_PER_CENT,
           title="SILVER BASELINE (C:XAGUSD, stop 40c / TP 25c / spread 2c, no Compass)")

    # --- spread sensitivity (Silver's real spread is uncertain; the closed-market quote was 10c) ---
    print("\n[SPREAD SENSITIVITY]  (Silver spread cost is the key unknown)")
    print("  spread |  trades  WR     PF     net(cents)  expectancy")
    for sp in (0.02, 0.05, 0.10):
        strategy_silver.SPREAD_POINTS = sp
        tr = _to_cents(run_baseline(verbose=False, strat_mod=strategy_silver, loader=silver_data.load))
        pts = [t["points"] for t in tr]
        w = [p for p in pts if p > 0]; l = [p for p in pts if p <= 0]
        print("   %2dc   |  %4d   %4.1f%%  %5.2f   %+8.1f    %+.2f pt"
              % (int(sp * 100), len(tr), 100.0 * len(w) / len(tr), _pf(w, l), sum(pts), sum(pts) / len(tr)))


if __name__ == "__main__":
    run()
