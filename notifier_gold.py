"""
GoldBase A.I. -- notifier_gold.py  (Percival)
================================================================================
AlbionBase push notifications via Pushover. EVERY notification is marked [DEMO] or [LIVE] from
trading_mode.json (Part 4a) so Nick always knows which Capital.com account is affected. Priorities
follow the 12 Aug 2026 brief (Part 4d): a LIVE event is always louder than the equivalent DEMO event.
Gated by LIVE_NOTIFICATIONS -- only AlbionBase is enabled; every other desk stays silent.
"""
import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

_ENV_PATH = BASE_DIR / ".env"
if _ENV_PATH.exists():
    load_dotenv(dotenv_path=_ENV_PATH)
else:
    load_dotenv()

import trading_mode                     # [DEMO]/[LIVE] marker source
try:
    import ledger                       # Pot + true Trading P&L (balance - net invested)
except Exception:
    ledger = None

log = logging.getLogger("GoldBase.Percival")

_PUSHOVER_API = "https://api.pushover.net/1/messages.json"
_USER  = os.getenv("PUSHOVER_USER_KEY",  "")
_TOKEN = os.getenv("PUSHOVER_API_TOKEN", "")
_LIVE_NOTIFICATIONS = os.getenv("LIVE_NOTIFICATIONS", "False").strip().lower() in ("1", "true", "yes", "on")
_NOTIONAL = os.getenv("NOTIONAL_CAPITAL", "3000")

# ── Per-instrument ────────────────────────────────────────────────────────────
SYS = "GoldBase"


def _px(p):
    try:
        return "$%s" % format(float(p), ",.2f")
    except Exception:
        return str(p)


# Pushover priority: -1 low, 0 normal, 1 high, 2 emergency (retry until acknowledged).
_P_LOW, _P_NORMAL, _P_HIGH, _P_EMERGENCY = -1, 0, 1, 2


def _m():
    try:
        return trading_mode.read_mode()
    except Exception:
        return "DEMO"


def _prio(demo, live):
    """LIVE events are always at least as loud as the DEMO equivalent (Part 4d)."""
    return live if _m() == "LIVE" else demo


def _money(v):
    try:
        v = float(v)
        return ("+£%.2f" % v) if v >= 0 else ("-£%.2f" % abs(v))
    except Exception:
        return str(v)


def _dur(d):
    if d is None:
        return ""
    if isinstance(d, str):
        return d
    try:
        s = int(d); h = s // 3600; m = (s % 3600) // 60
        return ("%dh %dm" % (h, m)) if h else ("%dm" % m)
    except Exception:
        return ""


def _pot_line(pot):
    """'\\nPot: £X | Trading P&L: +£Y' -- omitted if the pot isn't known."""
    if pot is None:
        return ""
    try:
        pot = float(pot)
        tp = ledger.trading_pnl(pot) if ledger else None
        tps = (" | Trading P&L: %s" % _money(tp)) if tp is not None else ""
        return "\nPot: £%s%s" % (format(pot, ",.2f"), tps)
    except Exception:
        return ""


def _send(title: str, message: str, priority: int = _P_NORMAL) -> None:
    """Post a Pushover notification, title auto-prefixed [DEMO]/[LIVE]. Silent unless LIVE_NOTIFICATIONS."""
    if not _LIVE_NOTIFICATIONS:
        log.debug("LIVE_NOTIFICATIONS off -- suppressed: %s", title)
        return
    if _m() != "LIVE":                    # STANDING RULE: Pushover ONLY in LIVE mode (K1 live box); DEMO = log-only
        log.debug("trading_mode DEMO -- Pushover suppressed (log only): %s", title)
        return
    if not _USER or not _TOKEN:
        log.debug("Pushover not configured -- skipping: %s", title)
        return
    full = "[%s] %s" % (_m(), title)
    data = {"token": _TOKEN, "user": _USER, "title": full, "message": message, "priority": priority}
    if priority >= _P_EMERGENCY:          # emergency must retry + expire
        data["retry"] = 60
        data["expire"] = 3600
    try:
        r = requests.post(_PUSHOVER_API, data=data, timeout=5)
        if r.status_code != 200:
            log.warning("Pushover HTTP %s for: %s", r.status_code, full)
    except Exception as exc:
        log.warning("Pushover notification failed (%s): %s", full, exc)


# ── Trading notifications (Part 1) ────────────────────────────────────────────
def notify_trade_opened(direction, entry_price, stop_loss, take_profit, stake,
                        liquidity_period="", size_oz=0.0) -> None:
    try:
        risk = float(stake) * abs(float(entry_price) - float(stop_loss))
    except Exception:
        risk = 0.0
    _send(
        "%s — %s opened" % (SYS, direction),
        "Entry: %s | Stop: %s | Stake: £%.2f/pt\nRisk: £%.0f (2%% of £%s)" % (
            _px(entry_price), _px(stop_loss), stake, risk, format(float(_NOTIONAL), ",.0f")),
        _prio(_P_LOW, _P_NORMAL),
    )


def notify_trade_closed_win(direction, exit_price, points_gained, pnl_gbp,
                            capital, reason, pot=None, duration=None) -> None:
    dur = _dur(duration)
    _send(
        "%s — %s closed ✅ %s" % (SYS, direction, _money(pnl_gbp)),
        "Exit: %s%s%s" % (_px(exit_price), (" | Duration: %s" % dur) if dur else "", _pot_line(pot)),
        _prio(_P_LOW, _P_NORMAL),
    )


def notify_trade_closed_loss(direction, exit_price, points_gained, pnl_gbp,
                             capital, reason, pot=None, duration=None) -> None:
    dur = _dur(duration)
    rtxt = (" | %s" % reason) if reason else ""
    _send(
        "%s — %s closed ❌ %s" % (SYS, direction, _money(pnl_gbp)),
        "Exit: %s%s%s%s" % (_px(exit_price), rtxt, (" | Duration: %s" % dur) if dur else "", _pot_line(pot)),
        _prio(_P_LOW, _P_NORMAL),
    )


def notify_two_speed_activated(activation_pts, locked_gbp, stop_price) -> None:
    _send(
        "%s — Trail tightened 🔒" % SYS,
        "Profit locked: £%.2f | Stop: %s" % (locked_gbp, _px(stop_price)),
        _prio(_P_LOW, _P_NORMAL),
    )


# ── System notifications (Part 2) ─────────────────────────────────────────────
def notify_kill_switch_triggered(tier, reason, wait_hours, daily_pnl, capital) -> None:
    limit = os.getenv("DAILY_LOSS_LIMIT_GBP", "180")
    _send(
        "%s — KILL SWITCH ⛔" % SYS,
        "Daily loss limit hit: -£%s\nNo new trades until tomorrow UTC" % limit,
        _prio(_P_NORMAL, _P_HIGH),
    )


def notify_external_close(direction, pnl_gbp, reason="unknown", pot=None) -> None:
    live = (_m() == "LIVE")
    _send(
        "%s — External close detected ⚠️" % SYS,
        "Position closed by Capital.com (not by engine)\nP&L: %s | Reason: %s\nCheck Capital.com account%s%s" % (
            _money(pnl_gbp), reason, (" immediately" if live else ""), _pot_line(pot)),
        _prio(_P_NORMAL, _P_HIGH),
    )


def notify_margin_rejection(reason="Insufficient margin") -> None:
    live = (_m() == "LIVE")
    _send(
        "%s — Order rejected ⚠️" % SYS,
        "Reason: %s\nSystem remains flat%s" % (reason, (" — check account balance" if live else "")),
        _prio(_P_NORMAL, _P_HIGH),
    )


def notify_system_restart(reason="crash") -> None:
    """Fired by the watchdog after it detects a crash/freeze and relaunches (Part 2)."""
    live = (_m() == "LIVE")
    _send(
        "%s — System restarted ⚠️" % SYS,
        "Watchdog detected crash and relaunched\n%s" % (
            "Check Capital.com for open positions immediately" if live else "Check dashboard for open positions"),
        _prio(_P_NORMAL, _P_EMERGENCY),
    )


def notify_kill_switch_reset(tier, wait_hours, capital) -> None:
    _send("%s — Trading resuming" % SYS,
          "Kill switch reset after %sh cooldown. Watching for setups." % wait_hours,
          _prio(_P_LOW, _P_NORMAL))


def notify_system_startup(capital, mode="") -> None:
    """Suppressed (Nick, 12 Aug 2026): a routine restart must NOT push. No-op so engine calls still work.
    A genuine CRASH still alerts -- via the watchdog's notify_system_restart, which is separate."""
    return


def notify_system_shutdown(capital) -> None:
    _send("%s — shutdown" % SYS, "%s stopped cleanly." % SYS, _P_LOW)


def notify_calendar_block(event_name, mins_remaining) -> None:
    _send("%s — calendar block" % SYS,
          "Trading paused: %s (%s min). Resumes automatically." % (event_name, mins_remaining), _P_LOW)


def notify_daily_summary(date_str, trades, pnl_gbp, capital, win_rate) -> None:
    _send("%s — daily %s" % (SYS, date_str),
          "Trades: %s | P&L: %s | WR: %.0f%%" % (trades, _money(pnl_gbp), win_rate), _P_LOW)


def notify_milestone_review(milestone_num) -> None:
    _send("%s — milestone #%s" % (SYS, milestone_num), "Review complete.", _P_LOW)


def notify_system_error(error_msg) -> None:
    _send("%s — system error ⚠️" % SYS, "Error: %s" % str(error_msg)[:200], _prio(_P_NORMAL, _P_HIGH))


def notify_event_block(reason) -> None:
    _send("%s — event block" % SYS, "High-impact event: %s. New entries paused." % reason, _P_LOW)


def notify_reconcile_clear() -> None:
    """One-shot confirmation that a previously-flagged order-reconcile mismatch has now cleared (18 Aug 2026).
    Low priority -- good news / FYI, not an action item. Pairs with live_executor.reconcile_alert_gate 'resolved'."""
    _send("%s -- reconcile clear" % SYS, "Order audit matches Capital.com again -- the earlier mismatch has cleared.", _P_LOW)
