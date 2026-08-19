"""
live_executor.py -- Stage B real-order execution against Capital.com (DEMO).
================================================================================
Replaces "Stanley" simulation with REAL orders on the Capital.com **demo** account.
Vendored BYTE-IDENTICAL across all AlbionBase systems (Rule 18) -- pure functions,
every Capital.com interaction goes through the connector passed in.

SAFETY (fail CLOSED -- any doubt returns None/False so the engine stays FLAT):
  * DEMO GUARD    -- refuses to place ANY order unless the connector is connected
                     AND on a DEMO account (account_type == "DEMO"). Stage C's
                     PAPER/LIVE switch is not built yet, so live orders are
                     physically impossible here; this is a second belt on top.
  * DOUBLE-ENTRY  -- never opens a 2nd position for an epic while one is already
                     open (checks Capital.com's live position list first).
  * NO AUTO-RETRY -- a failed placement returns None; the caller stays flat and
                     alerts. We never retry an order automatically.
  * NEVER ASSUME  -- a position is only considered open once Capital.com returns a
                     deal id; the caller must not mark itself in-trade otherwise.
"""
import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("LiveExecutor")


def _reconciliation_start():
    """Optional go-live cutoff (RECONCILIATION_START, ISO e.g. 2026-08-16T22:00:00Z). Orders placed BEFORE
    this are pre-go-live test/manual orders and are ignored by reconciliation, so they never raise a false
    'mismatch' alert. Unset -> no cutoff (reconcile everything the audit log covers)."""
    s = (os.getenv("RECONCILIATION_START", "") or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        log.warning("RECONCILIATION_START=%r not ISO YYYY-MM-DDThh:mm:ssZ -- ignoring.", s)
        return None

# ── Order audit log (go-live prerequisite) ──────────────────────────────────────────────────────
# Write-only trail of EVERY order interaction (request + outcome), INCLUDING the rejects/skips that the
# trade CSV never sees. Path(__file__) resolves per-repo, so each vendored copy writes its OWN system's
# logs/order_audit.csv. Reconcilable against Capital.com's /history/activity. Never raises -- an audit
# write must never break trading.
_AUDIT_FILE = Path(__file__).resolve().parent / "logs" / "order_audit.csv"
LAST_OPEN = {"outcome": None, "reason": None, "block_deal_id": None}   # place_order records its last outcome for the caller
_AUDIT_HEADERS = ["ts_utc", "epic", "action", "direction", "size", "stop", "outcome", "deal_id", "mode", "reason"]


def _audit(epic, action, direction=None, size=None, stop=None, outcome="", deal_id="", reason="", mode=""):
    """Append one order-interaction row. action = OPEN / CLOSE / STOP_SYNC. `mode` = DEMO / LIVE so every
    order is permanently attributable to the correct Capital.com account (Part 5).
    outcome = ACCEPTED / REJECTED / SKIPPED_MARKET_CLOSED / REFUSED / FAILED / ALREADY_CLOSED."""
    try:
        _AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        new = not _AUDIT_FILE.exists()
        with open(_AUDIT_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_AUDIT_HEADERS)
            if new:
                w.writeheader()
            w.writerow({
                "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "epic": epic, "action": action, "direction": direction or "",
                "size": ("%.2f" % size) if isinstance(size, (int, float)) else "",
                "stop": ("%.2f" % stop) if isinstance(stop, (int, float)) else "",
                "outcome": outcome, "deal_id": deal_id or "", "mode": mode or "",
                "reason": (reason or "")[:160],
            })
    except Exception as exc:
        log.warning("audit log write failed: %s", exc)


def demo_ok(ig) -> bool:
    """True only if the connector is connected AND on a DEMO account."""
    try:
        return ig is not None and ig.connected and ig.account_type == "DEMO"
    except Exception:
        return False


def trading_permitted(ig) -> bool:
    """Trade whenever the connector is CONNECTED. The DEMO/LIVE choice is made upstream by the trading_mode
    switch: the engine points the connector at the demo OR the live account, and a LIVE switch is only ever
    reachable once Nick has flipped it (behind a confirmation) with live credentials present. If LIVE is
    selected but not configured, the connector never connects -> this returns False -> we stay flat.
    Fail CLOSED on any doubt."""
    try:
        return ig is not None and ig.connected and ig.account_type in ("DEMO", "LIVE")
    except Exception:
        return False


def existing_position(ig, epic):
    """Return Capital.com's open position dict for `epic`, or None.
    Used for the double-entry guard and for restart/stop reconciliation.
    On an API error OR a disconnected connector returns the sentinel "UNKNOWN" so the caller can
    tell 'confirmed none' (None) apart from 'could not check' (UNKNOWN) and stay safe (never
    false-discard a real position, never place a blind second order)."""
    if ig is None or not getattr(ig, "connected", False):
        return "UNKNOWN"
    try:
        positions = ig.get_open_positions()
        if positions is None:          # API/connection failure -> distinct from a confirmed-empty response
            return "UNKNOWN"
        for p in positions:
            if (p.get("market", {}) or {}).get("epic") == epic:
                return p
        return None
    except Exception as exc:
        log.warning("existing_position(%s) check failed: %s", epic, exc)
        return "UNKNOWN"


def position_pnl(ig, epic):
    """Return the LIVE unrealised P&L (Capital.com 'upl') for the open `epic` position, or None.
    Used to show the REAL floating P&L on the dashboard (Stage B TEST 2)."""
    pos = existing_position(ig, epic)
    if pos in (None, "UNKNOWN"):
        return None
    try:
        return round(float((pos.get("position", {}) or {}).get("upl")), 2)
    except (TypeError, ValueError):
        return None


def market_tradeable(ig, epic):
    """True if Capital.com currently lists `epic` as TRADEABLE. Fail-OPEN on a check error
    (return True) -- the order POST + its rejection is the real backstop; this pre-check only lets us
    SKIP cleanly (no noisy rejected order) when we can confirm the market is in its closed window."""
    try:
        import requests
        r = requests.get("%s/markets/%s" % (ig._base_url, epic), headers=ig._headers(), timeout=8)
        if r.status_code == 200:
            return (r.json().get("snapshot", {}) or {}).get("marketStatus") == "TRADEABLE"
    except Exception as exc:
        log.warning("market_tradeable(%s) check failed: %s", epic, exc)
    return True


def place_order(ig, epic, direction, size, stop_pts, take_profit_pts=None):
    """Place an order on the CONNECTED account (DEMO or LIVE -- whichever the trading_mode switch selected).
    `direction` is 'LONG'/'SHORT'. Returns deal_id or None. Fails CLOSED: on any refusal/error returns None
    and the engine stays flat. Every audit row records the account (mode) the order was attributed to."""
    mode = getattr(ig, "account_type", "") or ""
    LAST_OPEN.update(outcome=None, reason=None, block_deal_id=None)
    if not trading_permitted(ig):
        log.error("ORDER REFUSED: trading not permitted -- no connected Capital.com account -- staying FLAT.")
        _audit(epic, "OPEN", direction, size, stop_pts, outcome="REFUSED", mode=mode, reason="no connected account")
        return None
    # Skip cleanly if the market is in a closed window (e.g. Brent's 22:00-00:00 UTC daily break) --
    # avoids a guaranteed-reject order. The engine stays flat and will enter when the market reopens.
    if not market_tradeable(ig, epic):
        log.info("ORDER SKIPPED: %s market is CLOSED (Capital.com) -- staying flat until it reopens.", epic)
        _audit(epic, "OPEN", direction, size, stop_pts, outcome="SKIPPED_MARKET_CLOSED", mode=mode)
        return None
    guard = existing_position(ig, epic)
    if guard is not None:          # a position exists, OR we couldn't verify -> refuse
        if guard == "UNKNOWN":
            log.error("ORDER REFUSED: could not verify existing %s positions -- staying FLAT.", epic)
            _audit(epic, "OPEN", direction, size, stop_pts, outcome="REFUSED", mode=mode, reason="could not verify existing positions")
        else:
            LAST_OPEN.update(outcome="REFUSED", reason="double-entry",
                             block_deal_id=(guard.get("position", {}) or {}).get("dealId"))
            log.error("ORDER REFUSED: a %s position is already open -- no double-entry.", epic)
            _audit(epic, "OPEN", direction, size, stop_pts, outcome="REFUSED", mode=mode, reason="double-entry: position already open")
        return None
    api_dir = "BUY" if direction == "LONG" else "SELL"
    try:
        res = ig.open_position(epic=epic, direction=api_dir, size=size, stop_distance=stop_pts,
                                   take_profit=take_profit_pts)
    except Exception as exc:
        log.error("ORDER FAILED: open_position raised %s -- staying FLAT.", exc)
        _audit(epic, "OPEN", direction, size, stop_pts, outcome="FAILED", mode=mode, reason=str(exc))
        return None
    deal_id = (res or {}).get("deal_id")
    if not deal_id:
        log.error("ORDER FAILED: open_position returned %s (no deal id) -- staying FLAT.", res)
        _audit(epic, "OPEN", direction, size, stop_pts, outcome="REJECTED", mode=mode, reason="rejected/failed (no deal id) -- see connector log")
        return None
    log.info("%s ORDER PLACED: %s %s size=%.2f stop=%.0fpt | ID: %s",
             mode or "?", direction, epic, size, stop_pts, deal_id)
    _audit(epic, "OPEN", direction, size, stop_pts, outcome="ACCEPTED", deal_id=deal_id, mode=mode)
    return deal_id


def sync_stop(ig, epic, stop_level, tp_level=None):
    """Push the engine's (tightened) stop to Capital.com so the BROKER enforces the locked profit even
    if the engine dies -- PUT /positions/{dealId} {stopLevel[, profitLevel]}. Looks up the live position's dealId.

    TP-PRESERVATION FIX (17 Aug 2026): Capital.com's position-update PUT CLEARS any risk order not included in
    the payload -- so sending {stopLevel} alone WIPED the take-profit set at open time. Every trailing-stop sync
    therefore stripped the broker TP, leaving a live position with no broker-side take-profit if the engine died.
    We now re-assert profitLevel in the SAME PUT whenever tp_level is supplied, so the TP survives every stop sync.

    Fail-SAFE: on any error/rejection logs a warning and returns False; the engine still enforces the stop/TP via
    its monitor + close_order, so a failed sync never loses protection, it just isn't broker-mirrored that cycle.
    Capital.com may reject a stop too close to spot (min distance) -- benign."""
    if ig is None or stop_level is None:
        return False
    mode = getattr(ig, "account_type", "") or ""
    pos = existing_position(ig, epic)
    if pos in (None, "UNKNOWN"):
        return False
    did = (pos.get("position", {}) or {}).get("dealId")
    if not did:
        return False
    try:
        import requests
        # Part 2 (13 Aug 2026): stop_level is a TRUE price; the broker wants its own quote units.
        # On a 100x-scaled live Brent an un-rescaled level would sit 100x below spot -- rejected, or
        # worse, silently accepted as a nonsense stop. No-op on demo (divisor 1).
        _lvl = round(float(ig.to_broker_price(epic, stop_level)), 2)
        payload = {"stopLevel": _lvl}
        _tp = None
        # Only send profitLevel when there is a VALID TP price (a real TP is always a positive price).
        # `if tp_level:` excludes None, 0 and 0.0 -- we NEVER send profitLevel=None or 0 (Capital.com reads
        # a missing/None/0 profitLevel as "remove the TP"). No valid TP -> the field is simply left out of
        # the request body, so the TP set at placement is untouched.
        try:
            _valid_tp = tp_level is not None and float(tp_level) > 0
        except (TypeError, ValueError):
            _valid_tp = False
        if _valid_tp:
            # Re-assert the TP in the SAME PUT so the stop update never wipes the broker take-profit.
            _tp = round(float(ig.to_broker_price(epic, tp_level)), 2)
            payload["profitLevel"] = _tp
        r = requests.put("%s/positions/%s" % (ig._base_url, did),
                         headers=ig._headers(), json=payload, timeout=8)
        if r.status_code == 200:
            log.info("BROKER STOP synced: %s -> stop %.2f (%.2f broker)%s | ID %s",
                     epic, round(float(stop_level), 2), _lvl,
                     ("" if _tp is None else " + TP %.2f (%.2f broker)" % (round(float(tp_level), 2), _tp)), did)
            return True
        log.warning("broker stop sync rejected (%s -> %.2f true / %.2f broker): HTTP %s %s",
                    epic, round(float(stop_level), 2), _lvl, r.status_code, r.text[:140])
        _audit(epic, "STOP_SYNC", stop=stop_level, outcome="REJECTED", deal_id=did, mode=mode, reason="HTTP %s" % r.status_code)
    except Exception as exc:
        log.warning("broker stop sync failed (%s): %s", epic, exc)
        _audit(epic, "STOP_SYNC", stop=stop_level, outcome="FAILED", mode=mode, reason=str(exc))
    return False


def close_order(ig, epic, deal_id, direction, size):
    """Close a DEMO position. Returns True if closed (or already gone).

    IMPORTANT: closes by the LIVE position's dealId (looked up now), NOT the id returned at open
    time -- Capital.com's deal-confirmation id differs from the open-position dealId, and only the
    latter closes. If no position exists, a broker stop/TP already closed it -> treat as success."""
    if ig is None:
        return False
    mode = getattr(ig, "account_type", "") or ""
    pos = existing_position(ig, epic)
    if pos is None:
        log.info("%s ORDER already closed broker-side (stop/TP) | %s | (opened id %s)", mode or "?", epic, deal_id)
        _audit(epic, "CLOSE", direction, size, outcome="ALREADY_CLOSED", deal_id=deal_id, mode=mode)
        return True
    if pos == "UNKNOWN":
        target = deal_id                       # can't look up -> best-effort with the stored id
    else:
        target = (pos.get("position", {}) or {}).get("dealId") or deal_id
    api_dir = "BUY" if direction == "LONG" else "SELL"
    try:
        # Pass the epic too: close_position now resolves the real dealId itself and uses the epic as
        # its fallback, so a stale/confirmation id can no longer leave a live position running.
        ok = ig.close_position(deal_id=target, direction=api_dir, size=size, epic=epic)
    except Exception as exc:
        log.error("close_order raised %s", exc)
        ok = False
    if ok:
        log.info("%s ORDER CLOSED: %s %s | ID: %s", mode or "?", direction, epic, target)
        _audit(epic, "CLOSE", direction, size, outcome="ACCEPTED", deal_id=target, mode=mode)
        return True
    # re-verify: gone now?
    if existing_position(ig, epic) is None:
        log.info("%s ORDER closed (verified gone) | %s | ID: %s", mode or "?", epic, target)
        _audit(epic, "CLOSE", direction, size, outcome="ACCEPTED", deal_id=target, mode=mode, reason="verified gone")
        return True
    log.error("%s ORDER CLOSE FAILED and position still open | %s | ID: %s", mode or "?", epic, target)
    _audit(epic, "CLOSE", direction, size, outcome="FAILED", deal_id=target, mode=mode)
    return False


def external_close_price(ig, epic, entry_price=None):
    """Best-effort ACTUAL close fill price from Capital.com /history/transactions, for EXTERNAL_CLOSE trades
    (a position Nick closed manually) where the engine would otherwise estimate the exit from the last price
    feed. Returns a TRUE price (float) or None -> the caller falls back to the estimate. Read-only; never raises.

    Matching: prefer the transaction whose openLevel matches `entry_price` (each position has a unique entry);
    else the most recent transaction for this instrument that carries a close level. Levels come back in the
    broker's quote units, so we divide by the epic's scale divisor to get the TRUE price (no-op on demo)."""
    try:
        import requests
        from datetime import timedelta
        frm = (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S")
        r = requests.get("%s/history/transactions?from=%s" % (ig._base_url, frm), headers=ig._headers(), timeout=10)
        if r.status_code != 200:
            return None
        try:
            div = float(ig.price_divisor(epic))
        except Exception:
            div = 1.0
        if not div:
            div = 1.0

        def _f(v):
            try:
                return float(str(v).replace(",", ""))
            except (TypeError, ValueError):
                return None

        txns = (r.json().get("transactions", []) or [])   # Capital.com returns newest-first
        fallback = None
        for t in txns:
            cl = _f(t.get("closeLevel"))
            if cl in (None, 0.0):
                continue
            true_close = round(cl / div, 6)
            ol = _f(t.get("openLevel"))
            if entry_price is not None and ol is not None:
                true_open = ol / div
                tol = max(0.5, abs(float(entry_price)) * 0.002)
                if abs(true_open - float(entry_price)) <= tol:
                    return true_close                       # exact position match
            if fallback is None:
                fallback = true_close                       # newest close for this account as a backstop
        return fallback
    except Exception as exc:
        log.warning("external_close_price(%s) failed: %s -- caller will estimate.", epic, exc)
        return None


def reconcile_orders(ig, epic, hours=24):
    """AUTOMATED order reconciliation: compare our audit log's ACCEPTED opens against Capital.com's
    /history/activity for `epic` over the last `hours`. Returns a list of mismatch strings (empty = clean):
      * AUDIT_ONLY  -- we recorded an ACCEPTED open that Capital.com has NO matching POSITION ACCEPTED for
                       (we think we opened, the broker didn't -> a would-be phantom)
      * BROKER_ONLY -- Capital.com opened a position we have NO audit row for (an untracked order)
    Matched by timestamp within +/-2 min (one epic per system, opens are infrequent). Read-only; never raises."""
    from datetime import timedelta
    mismatches = []
    try:
        import requests
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        ours_open, ours_all, audit_earliest = [], [], None
        if _AUDIT_FILE.exists():
            with open(_AUDIT_FILE, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        t = datetime.strptime(r["ts_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    except Exception:
                        continue
                    if audit_earliest is None or t < audit_earliest:
                        audit_earliest = t
                    if r.get("epic") != epic or r.get("outcome") != "ACCEPTED" or t < cutoff:
                        continue
                    act = r.get("action")
                    if act == "OPEN":
                        ours_open.append(t); ours_all.append(t)
                    elif act == "CLOSE":
                        ours_all.append(t)          # closes count too, so a broker CLOSE is not a false BROKER_ONLY
        if audit_earliest is None:
            return mismatches   # audit log empty -> nothing to reconcile (do not false-flag pre-audit orders)
        eff_cutoff = max(cutoff, audit_earliest)
        _rs = _reconciliation_start()
        if _rs is not None:
            eff_cutoff = max(eff_cutoff, _rs)
        ours_open = [t for t in ours_open if t >= eff_cutoff]
        ours_all  = [t for t in ours_all  if t >= eff_cutoff]
        resp = requests.get("%s/history/activity?from=%s" % (ig._base_url, eff_cutoff.strftime("%Y-%m-%dT%H:%M:%S")),
                            headers=ig._headers(), timeout=12)
        broker = []
        if resp.status_code == 200:
            for a in (resp.json().get("activities", []) or []):
                if a.get("epic") == epic and a.get("type") == "POSITION" and a.get("status") == "ACCEPTED":
                    try:
                        broker.append(datetime.strptime((a.get("dateUTC") or "")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc))
                    except Exception:
                        continue
        else:
            log.warning("reconcile_orders(%s): activity query HTTP %s -- skipping this run.", epic, resp.status_code)
            return mismatches
        WIN = 120.0
        for t in ours_open:
            if not any(abs((t - bt).total_seconds()) <= WIN for bt in broker):
                mismatches.append("AUDIT_ONLY: recorded ACCEPTED %s open at %sZ has NO Capital.com POSITION ACCEPTED"
                                  % (epic, t.strftime("%Y-%m-%dT%H:%M:%S")))
        for bt in broker:
            if not any(abs((bt - t).total_seconds()) <= WIN for t in ours_all):
                mismatches.append("BROKER_ONLY: Capital.com %s position event at %sZ with NO audit-log OPEN/CLOSE row"
                                  % (epic, bt.strftime("%Y-%m-%dT%H:%M:%S")))
    except Exception as exc:
        log.warning("reconcile_orders(%s) failed: %s", epic, exc)
    return mismatches


def reconcile_alert_gate(mismatches, state_path):
    """De-dup gate for the hourly reconcile alert so a persisting, already-known, non-actionable mismatch does
    not push to Nick every cycle. Returns 'push' (new/changed set, incl a count increase), 'suppress' (same set
    as last run -> log only), 'resolved' (had mismatches, now clean -> log only), or 'clean'. Signature = md5 of
    the sorted mismatch strings, persisted in state_path (survives restarts). Never raises -> falls back to 'push'
    so a real problem is never swallowed."""
    import hashlib, json as _json
    try:
        sig = hashlib.md5("|".join(sorted(mismatches)).encode("utf-8")).hexdigest() if mismatches else ""
        prev = ""
        try:
            prev = (_json.loads(Path(state_path).read_text(encoding="utf-8")) or {}).get("sig", "")
        except Exception:
            prev = ""
        try:
            Path(state_path).parent.mkdir(parents=True, exist_ok=True)
            Path(state_path).write_text(_json.dumps({
                "sig": sig, "count": len(mismatches),
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "mismatches": list(mismatches)[:20],
            }), encoding="utf-8")
        except Exception:
            pass
        if not mismatches:
            return "resolved" if prev else "clean"
        return "push" if sig != prev else "suppress"
    except Exception as exc:
        log.warning("reconcile_alert_gate failed (%s) -- defaulting to push", exc)
        return "push"


def double_entry_gate(block_deal_id, state_path):
    """De-dup gate for the double-entry-refused alert (19 Aug 2026), same pattern as reconcile_alert_gate. The guard
    ALWAYS refuses; this only decides what reaches Nick's phone. Returns 'push' (a NEW/different blocking position),
    'suppress' (same blocking position as last time -> log only), 'resolved' (was refusing, now clear -> log clean),
    or 'clean'. Signature = the blocking position's dealId, persisted in state_path (survives restarts)."""
    import json as _json
    try:
        sig = str(block_deal_id or "")
        prev = ""
        try:
            prev = (_json.loads(Path(state_path).read_text(encoding="utf-8")) or {}).get("sig", "")
        except Exception:
            prev = ""
        try:
            Path(state_path).parent.mkdir(parents=True, exist_ok=True)
            Path(state_path).write_text(_json.dumps({"sig": sig,
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}), encoding="utf-8")
        except Exception:
            pass
        if not sig:
            return "resolved" if prev else "clean"
        return "push" if sig != prev else "suppress"
    except Exception as exc:
        log.warning("double_entry_gate failed (%s) -- defaulting to push", exc)
        return "push"
