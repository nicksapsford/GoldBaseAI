#!/usr/bin/env python3
"""
ONE-TIME backfill (19 Aug 2026) -- correct today's understated EXTERNAL_CLOSE rows in the LIVE
GoldBase trade CSV, and add the 20:24 close that was never logged.

WHY: before v1.34.9, external_close_price() matched nothing in /history/transactions (that API has no
open/close LEVELS -- `size` IS the realised P&L) and the reconcile caller silently estimated the exit
from a stale price feed, understating each real close by 48-77%. Nick pulled the true realised P&L from
the broker. This script rewrites those rows to the REAL figures, exactly as the now-fixed code would have
written them (exit + points DERIVED from the real P&L via the engine's own price->P&L formula, so every
row stays internally consistent: pnl_gbp == (points - spread) * stake).

REAL realised P&L (GBP), today, LIVE account (from Capital.com):
    12:36  +37.29     13:31  +40.62     14:30  +43.46     20:24  +39.78 (never logged)
(17:22 was the phantom close already handled by the state-desync fix -- NOT touched here.)

SAFETY:
  * DRY-RUN by default -- prints every old->new change and the resulting LIVE daily total. Add --commit to write.
  * Backs up gold_trades.csv -> gold_trades.csv.bak.<UTC stamp> before writing.
  * Idempotent: a row already at its target (and the 20:24 row already present) is left untouched; safe to re-run.
  * Only ever touches today's LIVE EXTERNAL_CLOSE rows; every other row is copied through byte-for-byte.
  * Refuses to run unless the header already matches the 20-column CSV_HEADERS (start the service once first,
    v1.34.9+, so its header-repair runs) -- guarantees DictReader alignment before we touch anything.

RUN ON K1 (after `git pull` + a restart so the header is repaired):
    python backfill_20260819_gold_extclose.py            # dry run -- review the diff
    python backfill_20260819_gold_extclose.py --commit    # apply
Safe to delete after Nick confirms the LIVE daily total reads ~+£161.15.
"""
import csv
import sys
import shutil
from datetime import datetime, timezone
from pathlib import Path

# --- must match paper_trader_gold.py exactly ---
LOG_DIR      = Path(__file__).resolve().parent / "logs"
TRADES_LOG   = LOG_DIR / "gold_trades.csv"
CSV_HEADERS  = ["date", "time", "direction", "entry_price_usd", "exit_price_usd", "stake_per_point",
                "points_gained", "pnl_usd", "pnl_gbp", "gbpusd_rate", "exit_reason", "capital_after_gbp",
                "entry_time", "exit_time", "liquidity_period", "mae_pts", "mae_gbp", "mfe_pts", "mfe_gbp",
                "account"]
SPREAD_POINTS = 0.3                          # Capital.com gold spread (strategy_gold.SPREAD_POINTS)
TODAY         = "2026-08-19"
ACCOUNT       = "LIVE"

# time-prefix -> real realised P&L (GBP) for the three existing, understated EXTERNAL_CLOSE rows
CORRECTIONS = {"12:36": 37.29, "13:31": 40.62, "14:30": 43.46}

# The 20:24 close that was never logged (the position that stayed open through the 17:22 phantom, per the
# state-desync fix: BUY 1.5 Gold, entry 4487.05). pnl_gbp is the authoritative broker figure; exit + points
# are DERIVED from it so the row is self-consistent. Adjust ENTRY/SIZE/DIRECTION here if K1 records differ.
NEW_2024 = {
    "time_hms":   "20:24:00",
    "direction":  "LONG",
    "entry":      4487.05,
    "size":       1.5,
    "pnl_gbp":    39.78,
    "gbpusd":     None,        # unknown; filled from a neighbouring today row if available, else 0.00000
}


def _derive(entry, size, direction, target_pnl):
    """Return (exit_price, net_points) that reproduce target_pnl through calculate_pnl -- the inverse of
    pnl = (points - spread) * size, i.e. exit = entry +/- (pnl/size + spread)."""
    net_points = target_pnl / size
    raw        = net_points + SPREAD_POINTS
    exit_px    = entry + raw if direction == "LONG" else entry - raw
    return exit_px, net_points


def main():
    commit = "--commit" in sys.argv[1:]
    if not TRADES_LOG.exists():
        print("ABORT: %s not found." % TRADES_LOG); return 1

    with open(TRADES_LOG, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        print("ABORT: CSV empty."); return 1
    if rows[0] != CSV_HEADERS:
        print("ABORT: header is not the 20-column CSV_HEADERS -- start GoldBase v1.34.9+ once so its "
              "header-repair runs, then re-run this script.")
        print("  header now: %s" % rows[0]); return 1

    idx = {h: i for i, h in enumerate(CSV_HEADERS)}
    data = rows[1:]
    changes = []          # (label, old_pnl, new_pnl) for reporting
    a_gbpusd = ""         # borrow a real gbpusd rate from any today row for the synthetic 20:24 row

    for r in data:
        if len(r) >= 20 and r[idx["date"]] == TODAY and r[idx["gbpusd_rate"]]:
            a_gbpusd = a_gbpusd or r[idx["gbpusd_rate"]]

    # 1) correct the three existing understated rows (in place, self-consistent)
    for r in data:
        if len(r) < 20:
            continue
        if r[idx["date"]] != TODAY or r[idx["account"]] != ACCOUNT or r[idx["exit_reason"]] != "EXTERNAL_CLOSE":
            continue
        tprefix = r[idx["time"]][:5]
        if tprefix not in CORRECTIONS:
            continue
        target = CORRECTIONS[tprefix]
        old_pnl = float(r[idx["pnl_gbp"]])
        if abs(old_pnl - target) < 0.005:
            changes.append((tprefix, old_pnl, target, "already correct"))
            continue
        try:
            entry = float(r[idx["entry_price_usd"]]); size = float(r[idx["stake_per_point"]])
            direction = r[idx["direction"]]
            exit_px, pts = _derive(entry, size, direction, target)
            r[idx["exit_price_usd"]] = "%.2f" % exit_px
            r[idx["points_gained"]]  = "%+.2f" % pts
        except (ValueError, ZeroDivisionError):
            pass                                   # if entry/stake unreadable, still fix the P&L columns
        r[idx["pnl_gbp"]] = "%+.2f" % target
        r[idx["pnl_usd"]] = "%+.2f" % target       # Gold is GBP-settled -> pnl_usd == pnl_gbp
        changes.append((tprefix, old_pnl, target, "corrected"))

    # 2) add the 20:24 row if absent
    have_2024 = any(len(r) >= 20 and r[idx["date"]] == TODAY and r[idx["account"]] == ACCOUNT
                    and r[idx["exit_reason"]] == "EXTERNAL_CLOSE" and r[idx["time"]][:5] == "20:24"
                    for r in data)
    new_row = None
    if not have_2024:
        exit_px, pts = _derive(NEW_2024["entry"], NEW_2024["size"], NEW_2024["direction"], NEW_2024["pnl_gbp"])
        gbpusd = NEW_2024["gbpusd"] or a_gbpusd or "0.00000"
        rowd = {
            "date": TODAY, "time": NEW_2024["time_hms"], "direction": NEW_2024["direction"],
            "entry_price_usd": "%.2f" % NEW_2024["entry"], "exit_price_usd": "%.2f" % exit_px,
            "stake_per_point": "%.4f" % NEW_2024["size"], "points_gained": "%+.2f" % pts,
            "pnl_usd": "%+.2f" % NEW_2024["pnl_gbp"], "pnl_gbp": "%+.2f" % NEW_2024["pnl_gbp"],
            "gbpusd_rate": gbpusd, "exit_reason": "EXTERNAL_CLOSE", "capital_after_gbp": "",
            "entry_time": "", "exit_time": TODAY + " " + NEW_2024["time_hms"], "liquidity_period": "",
            "mae_pts": "0.00", "mae_gbp": "0.00", "mfe_pts": "0.00", "mfe_gbp": "0.00", "account": ACCOUNT,
        }
        new_row = [rowd[h] for h in CSV_HEADERS]

    # --- report ---
    print("=== GoldBase EXTERNAL_CLOSE backfill (%s) -- %s ===" % (TODAY, "COMMIT" if commit else "DRY RUN"))
    for tprefix, old, new, note in changes:
        print("  %s  £%+.2f -> £%+.2f   (%s)" % (tprefix, old, new, note))
    if new_row is not None:
        print("  20:24  ADD new row  £%+.2f  (exit %.2f derived from entry %.2f / %.1f)"
              % (NEW_2024["pnl_gbp"], float(new_row[idx["exit_price_usd"]]), NEW_2024["entry"], NEW_2024["size"]))
    else:
        print("  20:24  already present -- not adding")

    # resulting LIVE daily total (what the kill switch will seed)
    final = list(data) + ([new_row] if new_row is not None else [])
    total = sum(float(r[idx["pnl_gbp"]]) for r in final
                if len(r) >= 20 and r[idx["date"]] == TODAY and r[idx["account"]] == ACCOUNT and r[idx["pnl_gbp"]])
    print("  --> LIVE realised P&L for %s after backfill: £%+.2f  (target ~ +£161.15)" % (TODAY, total))

    did_something = any(n == "corrected" for *_ , n in changes) or new_row is not None
    if not did_something:
        print("Nothing to do -- already backfilled. (idempotent no-op)"); return 0
    if not commit:
        print("\nDRY RUN -- no file written. Re-run with --commit to apply."); return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = TRADES_LOG.with_suffix(".csv.bak.%s" % stamp)
    shutil.copy2(TRADES_LOG, backup)
    with open(TRADES_LOG, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADERS)
        w.writerows(final)
    print("\nWROTE %s  (backup: %s)" % (TRADES_LOG, backup.name))
    print("Restart is NOT required -- the running trader re-seeds daily_pnl_gbp from this CSV at next start,")
    print("but if a restart is convenient, do the Rule-12 broker-position check first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
