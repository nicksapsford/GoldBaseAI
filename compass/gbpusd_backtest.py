"""
compass/gbpusd_backtest.py -- GBPUSD (C:GBPUSD) Session-2 baseline backtest.
Same engine + Lancelot signal logic as Gold, driven with the GBPUSD strategy module (pip-scaled: 30-pip stop /
20-pip TP) and GBPUSD data. No Compass. Analysis only.

Units: the engine works in native price. GBPUSD "pip" = 0.0001, so price P&L is reported x10000 (pips) to match
the 30/20-pip spec and be comparable to Gold's point numbers.

Modelling note: the engine reuses Gold's session hours + 20:45 UTC daily force-close. FX actually trades a bit
later and through the 21:00-22:00 rollover, so this imposes a daily close on FX (same as the Gold strategy does)
-- a documented approximation, not a market necessity.
"""
import logging
logging.disable(logging.CRITICAL)

try:
    from . import gbpusd_data, strategy_gbpusd
    from .backtest_engine import run_baseline
    from .backtest_report import report, _pf
except ImportError:
    import gbpusd_data, strategy_gbpusd
    from backtest_engine import run_baseline
    from backtest_report import report, _pf

GBP_STAKE_PER_PIP = 0.667   # £20 risk / 30-pip stop = £0.667/pip (context only; PF/WR size-independent)


def _to_pips(trades):
    for t in trades:
        for k in ("points", "mfe", "mae", "upside_pts"):
            if t.get(k) is not None:
                t[k] = round(t[k] * 10000, 2)
    return trades


def run():
    gbpusd_data.ensure_data()

    gold = run_baseline(verbose=False)
    print(">> Gold regression (engine unchanged): %d trades, PF %.2f  (expect 849 / 1.15)"
          % (len(gold), _pf([t["points"] for t in gold if t["points"] > 0],
                            [t["points"] for t in gold if t["points"] <= 0])))

    strategy_gbpusd.SPREAD_POINTS = 0.0001   # 1 pip
    trades = _to_pips(run_baseline(verbose=False, strat_mod=strategy_gbpusd, loader=gbpusd_data.load))
    report(trades, stake=GBP_STAKE_PER_PIP, tp_level=20,
           title="GBPUSD BASELINE (C:GBPUSD, stop 30 pips / TP 20 pips / spread 1 pip, no Compass)")

    print("\n[SPREAD SENSITIVITY]  (pips)")
    print("  spread |  trades  WR     PF     net(pips)  expectancy")
    for sp in (0.00005, 0.0001, 0.0002):   # 0.5 / 1 / 2 pips
        strategy_gbpusd.SPREAD_POINTS = sp
        tr = _to_pips(run_baseline(verbose=False, strat_mod=strategy_gbpusd, loader=gbpusd_data.load))
        pts = [t["points"] for t in tr]
        w = [p for p in pts if p > 0]; l = [p for p in pts if p <= 0]
        print("  %.1fpip |  %4d   %4.1f%%  %5.2f   %+8.1f    %+.2f pip"
              % (sp * 10000, len(tr), 100.0 * len(w) / len(tr), _pf(w, l), sum(pts), sum(pts) / len(tr)))


if __name__ == "__main__":
    run()
