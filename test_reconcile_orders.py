"""
test_reconcile_orders.py -- verifies the 21 Aug 2026 reconcile_orders fix without touching Capital.com:
  BUG 1  ALREADY_CLOSED close rows were pre-filtered out (outcome != ACCEPTED) -> broker TP flagged BROKER_ONLY
  BUG 2  the +/-2 min match window was tighter than the engine's monitor-cycle detection lag (2m04s / 2m05s)
Stub-driven: the audit CSV and Capital.com /history/activity are faked. Run: python test_reconcile_orders.py
"""
import csv, tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import live_executor as le
import requests

EPIC = "GOLD"
Z = "%Y-%m-%dT%H:%M:%SZ"
NOW = datetime.now(timezone.utc)
def ago(mins, secs=0): return NOW - timedelta(minutes=mins, seconds=secs)
def zt(dt): return dt.strftime(Z)

PASS = []; FAIL = []
def check(name, cond):
    (PASS if cond else FAIL).append(name); print(("  PASS " if cond else "  FAIL ") + name)

le._reconciliation_start = lambda: None            # no reconciliation-start gating in the test
_ACT = {"list": []}
class FakeResp:
    status_code = 200
    def json(self): return {"activities": _ACT["list"]}
requests.get = lambda *a, **k: FakeResp()
class FakeIG:
    _base_url = "http://x"
    def _headers(self): return {}

def _broker(*events):   # events = (dt, )  -> POSITION ACCEPTED activity rows
    _ACT["list"] = [{"epic": EPIC, "type": "POSITION", "status": "ACCEPTED", "dateUTC": zt(dt)} for dt in events]

def _audit(rows):       # rows = list of (dt, action, outcome)
    f = Path(tempfile.mkdtemp()) / "order_audit.csv"
    with open(f, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(le._AUDIT_HEADERS)
        for dt, action, outcome in rows:
            w.writerow([zt(dt), EPIC, action, "LONG", 1.0, 0, outcome, "d1", "LIVE", ""])
    le._AUDIT_FILE = f

def run(win=300):
    return le.reconcile_orders(FakeIG(), EPIC, hours=24, match_window_s=win)

print("BUG REPRODUCTION -- broker TP close lands as ALREADY_CLOSED at a >2min detection lag:")
# 3 real-shaped cases: broker [open@-40m, close@-10m]; our OPEN ACCEPTED@-40m + CLOSE ALREADY_CLOSED at lag.
for lag_desc, lag in [("1m30s", timedelta(minutes=1, seconds=30)),
                      ("2m04s", timedelta(minutes=2, seconds=4)),
                      ("2m05s", timedelta(minutes=2, seconds=5))]:
    b_open, b_close = ago(40), ago(10)
    _broker(b_open, b_close)
    _audit([(b_open, "OPEN", "ACCEPTED"), (b_close + lag, "CLOSE", "ALREADY_CLOSED")])
    fixed = run(300)          # both fixes: ALREADY_CLOSED counted + 5-min window
    old_win = run(120)        # ALREADY_CLOSED counted but OLD 2-min window
    check("lag %s: FIXED (filter+5min) -> clean, no false BROKER_ONLY" % lag_desc, fixed == [])
    if lag < timedelta(minutes=2):
        check("lag %s: filter-fix alone (2min win) already clean" % lag_desc, old_win == [])
    else:
        check("lag %s: proves the WINDOW bug -- still flags at 2min, needs 5min" % lag_desc,
              any("BROKER_ONLY" in m for m in old_win))

print("GENUINE PROBLEMS still caught (fix must not suppress real issues):")
# Genuine phantom: we recorded an OPEN, broker has NOTHING.
_broker(); _audit([(ago(15), "OPEN", "ACCEPTED")])
check("genuine phantom -> AUDIT_ONLY still flagged", any("AUDIT_ONLY" in m for m in run(300)))

# Genuine BROKER_ONLY: broker opened a position we have NO audit row for at all.
_broker(ago(12)); _audit([(ago(600), "OPEN", "ACCEPTED")])   # our only row is 10h earlier, unrelated
check("untracked broker open -> BROKER_ONLY still flagged", any("BROKER_ONLY" in m for m in run(300)))

# Genuinely MISSING close: broker [open@-40m, close@-10m], we have the OPEN but NO close row at all.
_broker(ago(40), ago(10)); _audit([(ago(40), "OPEN", "ACCEPTED")])
check("missing close row -> BROKER_ONLY still flagged (not silently forgiven)",
      any("BROKER_ONLY" in m for m in run(300)))

# Outer bound: a close so lagged it exceeds even the 5-min window -> still flagged.
b_open, b_close = ago(40), ago(10)
_broker(b_open, b_close)
_audit([(b_open, "OPEN", "ACCEPTED"), (b_close + timedelta(minutes=6), "CLOSE", "ALREADY_CLOSED")])
check("close lagged 6min (> 5min window) -> BROKER_ONLY still flagged (outer bound holds)",
      any("BROKER_ONLY" in m for m in run(300)))

# A REJECTED open must still NOT count as a real position (unchanged intent).
_broker(); _audit([(ago(15), "OPEN", "REJECTED")])
check("REJECTED open is not counted as ours (no AUDIT_ONLY, nothing to match)", run(300) == [])

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILURES:", FAIL); raise SystemExit(1)
print("ALL GREEN")
