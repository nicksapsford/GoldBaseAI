"""
test_livesafety_gold.py -- verifies the two 20 Aug 2026 live-safety fixes without touching Capital.com:
  FIX 1  orphan-adoption fires on the DEMO->LIVE flip (not only startup)   [_sync_account]
  FIX 2  engine-initiated live closes book the REAL fill, not a feed estimate [close_trade]
Stub/spy driven (external_close_price + close_order are patched); the underlying reconcile_live_position
and external_close_price were already built + proven live. Run:  python test_livesafety_gold.py
"""
import os
os.environ.setdefault("LIVE_EXECUTION", "False")   # we flip the module flag directly per-test

import main_goldbase as mg
import paper_trader_gold as pt
import trading_mode
from strategy_gold import GoldTrade, TRAILING_STOP_POINTS

PASS = []; FAIL = []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name)

# ─────────────────────────── FIX 1: adopt on DEMO->LIVE flip ───────────────────────────
print("FIX 1 -- adoption fires at every transition onto LIVE (_sync_account):")

class SpyStanley:
    def __init__(self, in_trade=False, result=None):
        self.in_trade = in_trade; self.calls = 0; self._result = result
    def reconcile_live_position(self):
        self.calls += 1; return self._result

class FakeIG:
    def __init__(self, acct): self.account_type = acct
    def set_account(self, m): self.account_type = m; return True

_orig_read = trading_mode.read_mode
def _set_mode(m): trading_mode.read_mode = lambda: m

# Case 1: DEMO -> LIVE, engine flat  => adoption runs
_set_mode("LIVE"); ig = FakeIG("DEMO"); st = SpyStanley(result={"adopted": False})
mg._sync_account(ig, st, None)
check("DEMO->LIVE flip runs reconcile exactly once", st.calls == 1 and ig.account_type == "LIVE")

# Case 2: LIVE -> DEMO  => no adoption (only ->LIVE transitions adopt)
_set_mode("DEMO"); ig = FakeIG("LIVE"); st = SpyStanley()
mg._sync_account(ig, st, None)
check("LIVE->DEMO flip does NOT run adoption", st.calls == 0 and ig.account_type == "DEMO")

# Case 3: already LIVE (no switch) => no adoption
_set_mode("LIVE"); ig = FakeIG("LIVE"); st = SpyStanley()
mg._sync_account(ig, st, None)
check("no-op (already LIVE) does NOT re-adopt", st.calls == 0)

# Case 4: in_trade => switch blocked, no adoption
_set_mode("LIVE"); ig = FakeIG("DEMO"); st = SpyStanley(in_trade=True)
mg._sync_account(ig, st, None)
check("in-trade blocks the switch (no adoption, stays DEMO)", st.calls == 0 and ig.account_type == "DEMO")

# Case 5: an ADOPTED result fires the adoption notice (no crash)
fired = {"adopt": 0}
mg.notify_position_adopted = lambda *a, **k: fired.__setitem__("adopt", fired["adopt"] + 1)
_set_mode("LIVE"); ig = FakeIG("DEMO")
st = SpyStanley(result={"adopted": True, "direction": "LONG", "entry_price": 4380.0,
                        "stop_loss": 4360.0, "take_profit": 4420.0, "stake": 1.0})
mg._sync_account(ig, st, None)
check("adopted position on flip fires ONE adoption notification", st.calls == 1 and fired["adopt"] == 1)
trading_mode.read_mode = _orig_read

# ─────────────────────────── FIX 2: real-fill booking in close_trade ───────────────────────────
print("FIX 2 -- engine-initiated live closes book the REAL fill (close_trade):")
pt.LIVE_EXECUTION = True
pt.close_order = lambda *a, **k: None            # don't hit the broker
REAL_FILL = 4400.0                               # pretend the actual closing txn implies this exit
spy = {"n": 0}
def _fake_real(ig, epic, trade, **k):
    spy["n"] += 1; return REAL_FILL
pt._real_close_fill = _fake_real

def _mk_trader():
    st = pt.PaperTraderGold(); st.ig = object()
    # no-op persistence so the test never writes to the real trade log / state files
    st._log_trade = lambda *a, **k: None
    st._save_summary = lambda *a, **k: None
    st._save_state = lambda *a, **k: None
    st._clear_state = lambda *a, **k: None
    tr = GoldTrade(direction="LONG", entry_price=4380.0, stop_pts=TRAILING_STOP_POINTS,
                   size_oz=1.0, gbpusd_entry=1.27)
    tr.deal_id = "DEAL1"
    st.current_trade = tr; st._gbpusd = 1.27
    return st

ESTIMATE = 4390.0
for reason, expect_realfill in [("FORCE_CLOSE_2045", True), ("TAKE_PROFIT", True),
                                ("EXTERNAL_CLOSE", False), ("STOP_LOSS", False)]:
    spy["n"] = 0
    st = _mk_trader()
    tr = st.close_trade(ESTIMATE, reason, 1.27)
    booked = getattr(tr, "exit_price", None)
    if expect_realfill:
        check("%s books the REAL fill (%.0f not estimate %.0f) via lookup"
              % (reason, REAL_FILL, ESTIMATE), spy["n"] == 1 and abs(booked - REAL_FILL) < 1e-6)
    else:
        # EXTERNAL_CLOSE keeps the caller's real fill; STOP_LOSS keeps its deliberate clamp -- neither looks up
        check("%s does NOT re-look-up the fill (excluded)" % reason, spy["n"] == 0)

# Case: real fill not visible in time => falls back to the estimate, no crash
spy["n"] = 0
pt._real_close_fill = lambda ig, epic, trade, **k: (spy.__setitem__("n", spy["n"] + 1) or None)
pt.notify_system_error = lambda *a, **k: None
st = _mk_trader()
tr = st.close_trade(ESTIMATE, "FORCE_CLOSE_2045", 1.27)
check("force-close falls back to feed estimate when txn not visible (no crash)",
      spy["n"] == 1 and abs(getattr(tr, "exit_price", 0) - ESTIMATE) < 1e-6)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILURES:", FAIL); raise SystemExit(1)
print("ALL GREEN")
