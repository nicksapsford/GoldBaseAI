"""
compass/eurusd_backtest.py -- EURUSD (C:EURUSD) Session-2 baseline scan.
Reuses the pip-scaled, 6dp-fixed FX strategy module (strategy_gbpusd: 30-pip stop / 20-pip TP) with EURUSD data.
Same engine + Lancelot signal as Gold. No Compass. Analysis only. (Same modelling caveats as GBPUSD.)
"""
import logging
logging.disable(logging.CRITICAL)

try:
    from . import eurusd_data, strategy_gbpusd as fx
    from .backtest_engine import run_baseline
    from .backtest_report import report, _pf
except ImportError:
    import eurusd_data, strategy_gbpusd as fx
    from backtest_engine import run_baseline
    from backtest_report import report, _pf

STAKE_PER_PIP = 0.667


def _to_pips(trades):
    for t in trades:
        for k in ("points", "mfe", "mae", "upside_pts"):
            if t.get(k) is not None:
                t[k] = round(t[k] * 10000, 2)
    return trades


def run():
    eurusd_data.ensure_data()
    gold = run_baseline(verbose=False)
    print(">> Gold regression: %d trades, PF %.2f (expect 849 / 1.15)"
          % (len(gold), _pf([t["points"] for t in gold if t["points"] > 0],
                            [t["points"] for t in gold if t["points"] <= 0])))

    fx.TRAILING_STOP_POINTS = 0.0030
    fx.calculate_take_profit.__defaults__ = (0.0020,)
    fx.TIGHT_TRAIL_ACTIVATE_POINTS = 0.00225
    fx.TIGHT_TRAIL_POINTS = 0.00075
    fx.SPREAD_POINTS = 0.0001
    trades = _to_pips(run_baseline(verbose=False, strat_mod=fx, loader=eurusd_data.load))
    report(trades, stake=STAKE_PER_PIP, tp_level=20,
           title="EURUSD BASELINE (C:EURUSD, stop 30 pips / TP 20 pips / spread 1 pip, no Compass)")

    pf = _pf([t["points"] for t in trades if t["points"] > 0], [t["points"] for t in trades if t["points"] <= 0])
    if pf > 1.0:
        print("\n[STOP SWEEP -- PF>1.0, worth checking]  (pips, spread 1 pip)")
        print("  config       trades   WR      PF     net(pips)")
        for stop, tp, lbl in [(0.0030, 0.0020, "30/20"), (0.0045, 0.0030, "45/30"), (0.0060, 0.0040, "60/40")]:
            fx.TRAILING_STOP_POINTS = stop
            fx.calculate_take_profit.__defaults__ = (tp,)
            fx.TIGHT_TRAIL_ACTIVATE_POINTS = round(stop * 0.75, 6)
            fx.TIGHT_TRAIL_POINTS = round(stop * 0.25, 6)
            tr = _to_pips(run_baseline(verbose=False, strat_mod=fx, loader=eurusd_data.load))
            pts = [t["points"] for t in tr]; w = [p for p in pts if p > 0]; l = [p for p in pts if p <= 0]
            print("  %-11s  %4d   %4.1f%%  %5.2f   %+8.1f" % (lbl, len(tr), 100.0 * len(w) / len(tr), _pf(w, l), sum(pts)))
    else:
        print("\n(PF <= 1.0 -- no stop sweep per the brief; raw baseline reported.)")


if __name__ == "__main__":
    run()
