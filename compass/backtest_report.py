"""
compass/backtest_report.py -- prints the baseline backtest report (Merlin's Compass Session 2).
Metrics are in POINTS (size-independent); GBP shown at the live £1.50/pt stake for context.
"""
from collections import Counter

LIVE_STAKE_GBP = 1.50   # live Dell/K1 £/pt (context only; PF/win-rate are size-independent)


def _pf(wins_pts, losses_pts):
    gross_w = sum(wins_pts)
    gross_l = abs(sum(losses_pts))
    return (gross_w / gross_l) if gross_l else float("inf")


def report(trades: list):
    bar = "=" * 66
    print("\n" + bar); print("MERLIN'S COMPASS -- SESSION 2 BASELINE BACKTEST (Gold, no Compass)"); print(bar)
    if not trades:
        print("no trades produced -- check the data / warmup."); return

    pts = [t["points"] for t in trades]
    wins = [p for p in pts if p > 0]
    losses = [p for p in pts if p <= 0]
    n = len(trades)
    wr = 100.0 * len(wins) / n
    pf = _pf(wins, losses)
    net = sum(pts)
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    expectancy = net / n

    print("\n[OVERVIEW]")
    print("  trades              : %d   (LONG %d / SHORT %d)"
          % (n, sum(t["direction"] == "LONG" for t in trades), sum(t["direction"] == "SHORT" for t in trades)))
    print("  date range          : %s  ->  %s" % (trades[0]["entry_time"][:10], trades[-1]["entry_time"][:10]))
    print("  win rate            : %.1f%%   (%d win / %d loss)" % (wr, len(wins), len(losses)))
    print("  PROFIT FACTOR       : %.2f" % pf)
    print("  net points          : %+.1f pt   (= £%+.2f at £%.2f/pt)" % (net, net * LIVE_STAKE_GBP, LIVE_STAKE_GBP))
    print("  expectancy / trade  : %+.2f pt   (= £%+.2f)" % (expectancy, expectancy * LIVE_STAKE_GBP))
    print("  avg win / avg loss  : +%.1f pt / %.1f pt   (payoff %.2f)"
          % (avg_w, avg_l, (avg_w / abs(avg_l)) if avg_l else float("inf")))
    print("  best / worst trade  : %+.1f pt / %+.1f pt" % (max(pts), min(pts)))

    print("\n[EXIT REASONS]")
    by_reason = Counter(t["reason"] for t in trades)
    for r in ("TAKE_PROFIT", "STOP_LOSS", "FORCE_CLOSE"):
        rt = [t for t in trades if t["reason"] == r]
        if rt:
            rp = [t["points"] for t in rt]
            print("  %-12s : %4d (%4.1f%%)  avg %+6.1f pt  net %+8.1f pt"
                  % (r, len(rt), 100.0 * len(rt) / n, sum(rp) / len(rp), sum(rp)))

    print("\n[MFE DISTRIBUTION]  (peak favourable excursion per trade, points)")
    mfes = sorted(t["mfe"] for t in trades)
    def pctl(p):
        return mfes[min(len(mfes) - 1, int(p / 100.0 * len(mfes)))]
    print("  mean %.1f | median %.1f | p25 %.1f | p75 %.1f | p90 %.1f | max %.1f"
          % (sum(mfes) / len(mfes), pctl(50), pctl(25), pctl(75), pctl(90), max(mfes)))
    buckets = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 40), (40, 60), (60, 9999)]
    print("  bucket(pt)   count    %%    cumulative %% reaching >= low")
    total = len(mfes)
    for lo, hi in buckets:
        c = sum(1 for m in mfes if lo <= m < hi)
        reach = sum(1 for m in mfes if m >= lo)
        print("   %3d-%-4s   %5d  %5.1f%%      %5.1f%%" % (lo, (hi if hi < 9999 else "+"), c,
                                                           100.0 * c / total, 100.0 * reach / total))
    print("  --> %% of trades whose peak reached the 25pt TP level: %.1f%%"
          % (100.0 * sum(1 for m in mfes if m >= 25) / total))

    print("\n[YEARLY STABILITY]")
    for yr in sorted({t["entry_time"][:4] for t in trades}):
        yt = [t for t in trades if t["entry_time"][:4] == yr]
        yp = [t["points"] for t in yt]
        yw = [p for p in yp if p > 0]; yl = [p for p in yp if p <= 0]
        print("  %s : %4d trades  WR %4.1f%%  PF %.2f  net %+8.1f pt"
              % (yr, len(yt), 100.0 * len(yw) / len(yt), _pf(yw, yl), sum(yp)))
    print(bar)


if __name__ == "__main__":
    from compass.backtest_engine import run_baseline
    report(run_baseline())
