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
import logging

log = logging.getLogger("LiveExecutor")


def demo_ok(ig) -> bool:
    """True only if the connector is connected AND on a DEMO account."""
    try:
        return ig is not None and ig.connected and ig.account_type == "DEMO"
    except Exception:
        return False


def existing_position(ig, epic):
    """Return Capital.com's open position dict for `epic`, or None.
    Used for the double-entry guard and for restart/stop reconciliation.
    On an API error returns the sentinel string "UNKNOWN" so the caller can tell
    'confirmed none' (None) apart from 'could not check' (UNKNOWN) and stay safe."""
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


def place_order(ig, epic, direction, size, stop_pts):
    """Place a DEMO order. `direction` is 'LONG'/'SHORT'. Returns deal_id or None.
    Fails CLOSED: on any refusal/error returns None and the engine stays flat."""
    if not demo_ok(ig):
        log.error("ORDER REFUSED: connector is not a connected DEMO account -- staying FLAT.")
        return None
    guard = existing_position(ig, epic)
    if guard is not None:          # a position exists, OR we couldn't verify -> refuse
        if guard == "UNKNOWN":
            log.error("ORDER REFUSED: could not verify existing %s positions -- staying FLAT.", epic)
        else:
            log.error("ORDER REFUSED: a %s position is already open -- no double-entry.", epic)
        return None
    api_dir = "BUY" if direction == "LONG" else "SELL"
    try:
        res = ig.open_position(epic=epic, direction=api_dir, size=size, stop_distance=stop_pts)
    except Exception as exc:
        log.error("ORDER FAILED: open_position raised %s -- staying FLAT.", exc)
        return None
    deal_id = (res or {}).get("deal_id")
    if not deal_id:
        log.error("ORDER FAILED: open_position returned %s (no deal id) -- staying FLAT.", res)
        return None
    log.info("DEMO ORDER PLACED: %s %s size=%.2f stop=%.0fpt | ID: %s",
             direction, epic, size, stop_pts, deal_id)
    return deal_id


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
        return True
    # re-verify: gone now?
    if existing_position(ig, epic) is None:
        log.info("DEMO ORDER closed (verified gone) | %s | ID: %s", epic, target)
        return True
    log.error("DEMO ORDER CLOSE FAILED and position still open | %s | ID: %s", epic, target)
    return False
