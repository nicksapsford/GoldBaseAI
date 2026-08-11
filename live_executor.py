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
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("LiveExecutor")

# ── Order audit log (go-live prerequisite) ──────────────────────────────────────────────────────
# Write-only trail of EVERY order interaction (request + outcome), INCLUDING the rejects/skips that the
# trade CSV never sees. Path(__file__) resolves per-repo, so each vendored copy writes its OWN system's
# logs/order_audit.csv. Reconcilable against Capital.com's /history/activity. Never raises -- an audit
# write must never break trading.
_AUDIT_FILE = Path(__file__).resolve().parent / "logs" / "order_audit.csv"
_AUDIT_HEADERS = ["ts_utc", "epic", "action", "direction", "size", "stop", "outcome", "deal_id", "reason"]


def _audit(epic, action, direction=None, size=None, stop=None, outcome="", deal_id="", reason=""):
    """Append one order-interaction row. action = OPEN / CLOSE / STOP_SYNC.
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
                "outcome": outcome, "deal_id": deal_id or "", "reason": (reason or "")[:160],
            })
    except Exception as exc:
        log.warning("audit log write failed: %s", exc)


def demo_ok(ig) -> bool:
    """True only if the connector is connected AND on a DEMO account."""
    try:
        return ig is not None and ig.connected and ig.account_type == "DEMO"
    except Exception:
        return False


def trading_permitted(ig, allow_live=False) -> bool:
    """The Stage C order gate. A DEMO account may ALWAYS trade (demo is never real money). A LIVE account
    may trade ONLY when `allow_live` is True -- the caller has confirmed mode==LIVE AND ALLOW_LIVE_TRADING
    (Layers 1+2 of the safety chain). Requires a connected account. Fail CLOSED on any doubt."""
    try:
        if ig is None or not ig.connected:
            return False
        acc = ig.account_type
        if acc == "DEMO":
            return True
        if acc == "LIVE":
            return bool(allow_live)
        return False
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
        for p in (ig.get_open_positions() or []):
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


def place_order(ig, epic, direction, size, stop_pts, allow_live=False):
    """Place an order on the connected account. `direction` is 'LONG'/'SHORT'. Returns deal_id or None.
    A DEMO account always trades; a LIVE account requires allow_live (mode==LIVE AND ALLOW_LIVE_TRADING).
    Fails CLOSED: on any refusal/error returns None and the engine stays flat."""
    if not trading_permitted(ig, allow_live):
        log.error("ORDER REFUSED: trading not permitted -- not a connected DEMO account, or LIVE without "
                  "the safety chain (mode/ALLOW_LIVE_TRADING) -- staying FLAT.")
        _audit(epic, "OPEN", direction, size, stop_pts, outcome="REFUSED", reason="trading not permitted (account / live-lock)")
        return None
    # Skip cleanly if the market is in a closed window (e.g. Brent's 22:00-00:00 UTC daily break) --
    # avoids a guaranteed-reject order. The engine stays flat and will enter when the market reopens.
    if not market_tradeable(ig, epic):
        log.info("ORDER SKIPPED: %s market is CLOSED (Capital.com) -- staying flat until it reopens.", epic)
        _audit(epic, "OPEN", direction, size, stop_pts, outcome="SKIPPED_MARKET_CLOSED")
        return None
    guard = existing_position(ig, epic)
    if guard is not None:          # a position exists, OR we couldn't verify -> refuse
        if guard == "UNKNOWN":
            log.error("ORDER REFUSED: could not verify existing %s positions -- staying FLAT.", epic)
            _audit(epic, "OPEN", direction, size, stop_pts, outcome="REFUSED", reason="could not verify existing positions")
        else:
            log.error("ORDER REFUSED: a %s position is already open -- no double-entry.", epic)
            _audit(epic, "OPEN", direction, size, stop_pts, outcome="REFUSED", reason="double-entry: position already open")
        return None
    api_dir = "BUY" if direction == "LONG" else "SELL"
    try:
        res = ig.open_position(epic=epic, direction=api_dir, size=size, stop_distance=stop_pts)
    except Exception as exc:
        log.error("ORDER FAILED: open_position raised %s -- staying FLAT.", exc)
        _audit(epic, "OPEN", direction, size, stop_pts, outcome="FAILED", reason=str(exc))
        return None
    deal_id = (res or {}).get("deal_id")
    if not deal_id:
        log.error("ORDER FAILED: open_position returned %s (no deal id) -- staying FLAT.", res)
        _audit(epic, "OPEN", direction, size, stop_pts, outcome="REJECTED", reason="rejected/failed (no deal id) -- see connector log")
        return None
    log.info("DEMO ORDER PLACED: %s %s size=%.2f stop=%.0fpt | ID: %s",
             direction, epic, size, stop_pts, deal_id)
    _audit(epic, "OPEN", direction, size, stop_pts, outcome="ACCEPTED", deal_id=deal_id)
    return deal_id


def sync_stop(ig, epic, stop_level):
    """Push the engine's (tightened) stop to Capital.com so the BROKER enforces the locked profit even
    if the engine dies -- PUT /positions/{dealId} {stopLevel}. Looks up the live position's dealId.
    Fail-SAFE: on any error/rejection logs a warning and returns False; the engine still enforces the
    stop via its monitor + close_order, so a failed sync never loses protection, it just isn't
    broker-mirrored that cycle. Capital.com may reject a stop too close to spot (min distance) -- benign."""
    if ig is None or stop_level is None:
        return False
    pos = existing_position(ig, epic)
    if pos in (None, "UNKNOWN"):
        return False
    did = (pos.get("position", {}) or {}).get("dealId")
    if not did:
        return False
    try:
        import requests
        r = requests.put("%s/positions/%s" % (ig._base_url, did),
                         headers=ig._headers(), json={"stopLevel": round(float(stop_level), 2)}, timeout=8)
        if r.status_code == 200:
            log.info("BROKER STOP synced: %s -> %.2f | ID %s", epic, round(float(stop_level), 2), did)
            return True
        log.warning("broker stop sync rejected (%s -> %.2f): HTTP %s %s",
                    epic, round(float(stop_level), 2), r.status_code, r.text[:140])
        _audit(epic, "STOP_SYNC", stop=stop_level, outcome="REJECTED", deal_id=did, reason="HTTP %s" % r.status_code)
    except Exception as exc:
        log.warning("broker stop sync failed (%s): %s", epic, exc)
        _audit(epic, "STOP_SYNC", stop=stop_level, outcome="FAILED", reason=str(exc))
    return False


def close_order(ig, epic, deal_id, direction, size):
    """Close a DEMO position. Returns True if closed (or already gone).

    IMPORTANT: closes by the LIVE position's dealId (looked up now), NOT the id returned at open
    time -- Capital.com's deal-confirmation id differs from the open-position dealId, and only the
    latter closes. If no position exists, a broker stop/TP already closed it -> treat as success."""
    if ig is None:
        return False
    pos = existing_position(ig, epic)
    if pos is None:
        log.info("DEMO ORDER already closed broker-side (stop/TP) | %s | (opened id %s)", epic, deal_id)
        _audit(epic, "CLOSE", direction, size, outcome="ALREADY_CLOSED", deal_id=deal_id)
        return True
    if pos == "UNKNOWN":
        target = deal_id                       # can't look up -> best-effort with the stored id
    else:
        target = (pos.get("position", {}) or {}).get("dealId") or deal_id
    api_dir = "BUY" if direction == "LONG" else "SELL"
    try:
        ok = ig.close_position(deal_id=target, direction=api_dir, size=size)
    except Exception as exc:
        log.error("close_order raised %s", exc)
        ok = False
    if ok:
        log.info("DEMO ORDER CLOSED: %s %s | ID: %s", direction, epic, target)
        _audit(epic, "CLOSE", direction, size, outcome="ACCEPTED", deal_id=target)
        return True
    # re-verify: gone now?
    if existing_position(ig, epic) is None:
        log.info("DEMO ORDER closed (verified gone) | %s | ID: %s", epic, target)
        _audit(epic, "CLOSE", direction, size, outcome="ACCEPTED", deal_id=target, reason="verified gone")
        return True
    log.error("DEMO ORDER CLOSE FAILED and position still open | %s | ID: %s", epic, target)
    _audit(epic, "CLOSE", direction, size, outcome="FAILED", deal_id=target)
    return False


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
        ours, audit_earliest = [], None
        if _AUDIT_FILE.exists():
            with open(_AUDIT_FILE, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        t = datetime.strptime(r["ts_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    except Exception:
                        continue
                    if audit_earliest is None or t < audit_earliest:
                        audit_earliest = t
                    if r.get("epic") == epic and r.get("action") == "OPEN" and r.get("outcome") == "ACCEPTED" and t >= cutoff:
                        ours.append(t)
        if audit_earliest is None:
            return mismatches   # audit log empty -> nothing to reconcile (don't false-flag pre-audit orders)
        # Only reconcile the period the audit was actually recording (floor at its first entry) so orders
        # that predate the audit log never show up as false BROKER_ONLY mismatches.
        eff_cutoff = max(cutoff, audit_earliest)
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
        bmatched = [False] * len(broker)
        for t in ours:
            hit = False
            for i, bt in enumerate(broker):
                if not bmatched[i] and abs((t - bt).total_seconds()) <= WIN:
                    bmatched[i] = True
                    hit = True
                    break
            if not hit:
                mismatches.append("AUDIT_ONLY: recorded ACCEPTED %s open at %sZ has NO Capital.com POSITION ACCEPTED"
                                  % (epic, t.strftime("%Y-%m-%dT%H:%M:%S")))
        for i, bt in enumerate(broker):
            if not bmatched[i]:
                mismatches.append("BROKER_ONLY: Capital.com opened %s at %sZ with NO audit-log ACCEPTED row"
                                  % (epic, bt.strftime("%Y-%m-%dT%H:%M:%S")))
    except Exception as exc:
        log.warning("reconcile_orders(%s) failed: %s", epic, exc)
    return mismatches
