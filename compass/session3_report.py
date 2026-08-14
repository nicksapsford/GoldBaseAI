"""
compass/session3_report.py -- Merlin's Compass Session 3: apply the R:R gate + time gate to the SAME
849-trade baseline universe and report the delta.

Gates only REMOVE entries, so the kept set keeps its exact baseline outcomes. The key question:
what is the P&L of the trades each gate would BLOCK? Net-negative blocked P&L => the gate improves the system.
(Caveat: the 30-min post-loss cooldown means removing a losing trade could, in live, free a later entry --
a small second-order effect not modelled here; this evaluates the gates on the fixed 849-trade set.)
"""
import logging
logging.disable(logging.CRITICAL)

try:
    from .backtest_engine import run_baseline
except ImportError:
    from backtest_engine import run_baseline


def _pf(rows):
    w = sum(t["points"] for t in rows if t["points"] > 0)
    l = abs(sum(t["points"] for t in rows if t["points"] <= 0))
    return (w / l) if l else float("inf")


def _stats(rows):
    n = len(rows)
    if not n:
        return "  (none)"
    wins = sum(1 for t in rows if t["points"] > 0)
    net = sum(t["points"] for t in rows)
    return "n=%-4d  WR %5.1f%%  PF %5.2f  net %+8.1f pt  avg %+5.2f" % (
        n, 100.0 * wins / n, _pf(rows), net, net / n)


def _bucket(trades, key, edges, label):
    print("\n[%s]" % label)
    print("  %-14s %s" % ("bucket", "stats"))
    for lo, hi in zip(edges, edges[1:] + [float("inf")]):
        rows = [t for t in trades if t.get(key) is not None and lo <= t[key] < hi]
        rng = "%g-%g" % (lo, hi) if hi != float("inf") else "%g+" % lo
        print("  %-14s %s" % (rng, _stats(rows)))


def _gate_sweep(trades, key, thresholds, label, block_below=True):
    """Block trades where key < threshold (block_below) and report blocked-vs-kept."""
    print("\n[%s]" % label)
    base_net = sum(t["points"] for t in trades)
    print("  threshold |            BLOCKED            |             KEPT")
    for th in thresholds:
        if block_below:
            blocked = [t for t in trades if t.get(key) is not None and t[key] < th]
        else:
            blocked = [t for t in trades if t.get(key) is not None and t[key] >= th]
        kept = [t for t in trades if t not in blocked]
        bn = sum(t["points"] for t in blocked)
        kn = sum(t["points"] for t in kept)
        bwr = 100.0 * sum(1 for t in blocked if t["points"] > 0) / len(blocked) if blocked else 0
        print("   %-8s | n=%-4d net %+8.1f WR %4.1f%% | n=%-4d net %+8.1f PF %.2f  (baseline net %+.0f)"
              % (th, len(blocked), bn, bwr, len(kept), kn, _pf(kept), base_net))


def report():
    trades = run_baseline(verbose=False)
    bar = "=" * 74
    print("\n" + bar); print("MERLIN'S COMPASS -- SESSION 3: R:R GATE + TIME GATE vs BASELINE"); print(bar)
    print("\n[BASELINE recap]  " + _stats(trades))

    # ── 1. Does low upside-to-resistance actually predict losses? ──
    _bucket(trades, "upside_pts",
            [0, 2, 5, 10, 15, 20, 30], "R:R DIAGNOSTIC -- outcome by points-to-nearest-target (upside)")
    _bucket(trades, "rr", [0, 0.1, 0.25, 0.5, 0.75, 1.0], "R:R DIAGNOSTIC -- outcome by R:R ratio (upside/stop)")

    # ── 2. R:R gate sweep ──
    _gate_sweep(trades, "upside_pts", [2, 3, 5, 8, 10, 15],
                "R:R GATE -- block entries with upside-to-target < X pts")

    # ── 3. Time gate: outcome by minutes-to-force-close, then sweep ──
    _bucket(trades, "mins_to_fc",
            [0, 60, 120, 240, 480, 720], "TIME DIAGNOSTIC -- outcome by minutes to force-close at entry")
    _gate_sweep(trades, "mins_to_fc", [60, 90, 120, 180, 240],
                "TIME GATE -- block entries with < T minutes to force-close")

    print("\n" + bar)
    return trades


def combined(trades, upside_min, mins_min):
    """Apply both gates and print the final delta."""
    blocked = [t for t in trades
               if (t.get("upside_pts") is not None and t["upside_pts"] < upside_min)
               or (t.get("mins_to_fc") is not None and t["mins_to_fc"] < mins_min)]
    kept = [t for t in trades if t not in blocked]
    print("\n[COMBINED GATE]  upside >= %g pt AND mins_to_fc >= %g" % (upside_min, mins_min))
    print("  BLOCKED : " + _stats(blocked))
    print("  KEPT    : " + _stats(kept))
    print("  baseline net %+.1f pt -> kept net %+.1f pt (blocked removed %+.1f)"
          % (sum(t["points"] for t in trades), sum(t["points"] for t in kept),
             sum(t["points"] for t in blocked)))
    return blocked, kept


if __name__ == "__main__":
    t = report()
