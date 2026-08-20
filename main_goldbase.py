"""
main_goldbase.py -- GoldBase A.I. engine (Benchmark Desk)
================================================================================
Parallel scientific baseline for GoldTrader. NO Arthur (AI), NO Morgan
(confidence), NO Guinevere (news), NO phantom logging.

Decision engine, every 5m candle:

  STEP 1  All Lancelot pre-checks must pass       (identical to GoldTrader)
  STEP 2  Daily + 1h + 5m SSL must ALL agree      (the direction signal)
  STEP 3  Direction switch decides execution:
            WITH    -> trade the SSL direction
            AGAINST -> trade the opposite (contrarian). Lancelot validates the
                       SIGNAL direction; only the executed direction flips.

Exits: 30pt trailing stop / 50pt target / Profit Protection Ladder (Variant 2),
all handled by Stanley's monitor_trade(). Gold-specific Lancelot retained: Asian
session applies a tighter RSI (60/40) conviction filter for SHORTs.

Gold XAU/USD, Capital.com, port 5033, £1,000 paper, 0.3pt spread, session
22:00-21:00 UTC. Template: GoldTrader v1.2.7. All times UTC.
"""
import logging
import signal
import sys
import time
import random
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from data_feed_gold import GoldDataFeed, GOLD_EPIC, is_market_open, get_liquidity_period
# ── Camelot engine path seam: point the engine at THIS instrument dir + the AlbionBase root
# (parent) BEFORE importing any camelot_engine module, so .env / logs / trading_mode.json /
# investment_ledger.csv resolve to exactly the same files as the pre-engine build, whatever the CWD.
import os as _seam_os
from pathlib import Path as _seam_Path
_seam_os.environ.setdefault("ALBION_APP_DIR", str(_seam_Path(__file__).resolve().parent))
_seam_os.environ.setdefault("ALBION_ROOT", str(_seam_Path(__file__).resolve().parent.parent))
from camelot_engine.capitalcom_connector import CapitalComConnector
from notifier_gold import (
    notify_system_startup, notify_trade_opened,
    notify_trade_closed_win, notify_trade_closed_loss, notify_system_error,
    notify_two_speed_activated, notify_external_close, notify_margin_rejection,
    notify_kill_switch_triggered, notify_reconcile_clear, notify_position_adopted,
)
from paper_trader_gold import PaperTraderGold, TRADES_LOG, LIVE_EXECUTION as _LIVE_EXECUTION
from camelot_engine import live_executor
from camelot_engine import reconciliation
from camelot_engine import trading_mode
from pre_checks_gold import run_all_pre_checks, run_individual_pre_checks, check_kill_switch_reset
from strategy_gold import (should_force_close, get_gbpusd_rate, TIGHT_TRAIL_ACTIVATE_POINTS, SPREAD_POINTS,
                           NOTIONAL_CAPITAL, RISK_PCT)
from camelot_engine import direction_switch

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
_VER = BASE_DIR / "VERSION"
VERSION = _VER.read_text().strip() if _VER.exists() else "1.0.0"

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SHUTDOWN_FLAG = LOG_DIR / "shutdown.flag"
RECON_STATE_FILE = LOG_DIR / "reconcile_state.json"
DE_STATE_FILE = LOG_DIR / "double_entry_state.json"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logging.Formatter.converter = time.gmtime
log = logging.getLogger("GoldBase")

PORT = 5033
DASHBOARD_URL = "http://localhost:%d/api/update" % PORT
PAPER_TRADING_MODE = True
CANDLE_SECONDS = 300
MONITOR_SECONDS = 30

_SHUTDOWN = False


def _handle_signal(sig, frame):
    global _SHUTDOWN
    _SHUTDOWN = True
    log.info("Signal %s -- shutting down", sig)


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


from camelot_engine.account_state import AccountState, _today_realised_pnl


# ── SSL 3-timeframe agreement (the benchmark's direction signal) ──────────────

def _ssl(bar):
    if bar is None:
        return None
    v = bar.get("ssl_bull")
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return "LONG" if bool(v) else "SHORT"


def ssl_agreement(bar_1d, bar_1h, bar_5m):
    d, h, m = _ssl(bar_1d), _ssl(bar_1h), _ssl(bar_5m)
    if d is not None and d == h == m:
        return d
    return None


def _get_price(feed):
    try:
        return float(feed.latest_bar("5m")["close"])
    except Exception:
        return None


# ── State (last-known, for the dashboard) ─────────────────────────────────────

_last = {"price": None, "signal": None, "lancelot": "awaiting first tick",
         "checks": {}, "session": "--"}


def _open(stanley, ig, direction, price, period, gbpusd):
    trade = stanley.open_trade(direction, price, gbpusd, period)
    if trade is None:
        _lo = getattr(live_executor, "LAST_OPEN", {}) or {}
        if _lo.get("reason") == "double-entry":
            _g = live_executor.double_entry_gate(_lo.get("block_deal_id"), DE_STATE_FILE)
            log.error("OPEN blocked: a position is already open at Capital.com (double-entry, deal %s) -- engine FLAT "
                      "but broker isn't [state desync; alert=%s].", _lo.get("block_deal_id"), _g)
            if _g == "push":
                try:
                    notify_margin_rejection("GoldBase: a position is already open at Capital.com (no double-entry). Engine is "
                                            "FLAT but the broker is not -- state desync; check.")
                except Exception:
                    pass
        else:
            try:
                notify_margin_rejection("Order rejected -- see logs/order_audit.csv")
            except Exception:
                pass
            log.error("OPEN FAILED: real order not placed -- remaining FLAT.")
        return
    try:
        notify_trade_opened(direction=direction, entry_price=price, stop_loss=trade.stop_loss,
                            take_profit=trade.take_profit, stake=trade.stake,
                            liquidity_period=period, size_oz=trade.size_oz)
    except Exception as exc:
        log.warning("Percival open notify failed: %s", exc)
    log.info("OPEN %s @ $%.2f | stop=$%.2f target=$%.2f stake=£%.2f/pt",
             direction, price, trade.stop_loss, trade.take_profit, trade.stake)


def _on_close(stanley, account, price, reason):
    trade = stanley.trade_history[-1] if stanley.trade_history else None
    if trade is None:
        return
    account.record_trade(trade.pnl_gbp)
    _dur = None
    try:
        if getattr(trade, "entry_time", None):
            _dur = (datetime.now(timezone.utc) - trade.entry_time).total_seconds()
    except Exception:
        _dur = None
    _pot = reconciliation.last_balance()   # last-known real Capital.com balance (cached, no extra call)
    # Display the trade's ACTUAL booked exit (real fill for a live close), not the caller's feed estimate,
    # so the notification/log agree with the real-fill P&L booked in close_trade (20 Aug 2026).
    _disp = getattr(trade, "exit_price", None)
    if _disp is None:
        _disp = price
    try:
        if trade.pnl_gbp >= 0:
            notify_trade_closed_win(trade.direction, _disp, trade.points_gained,
                                    trade.pnl_gbp, account.capital_gbp, reason, pot=_pot, duration=_dur)
        else:
            notify_trade_closed_loss(trade.direction, _disp, trade.points_gained,
                                     trade.pnl_gbp, account.capital_gbp, reason, pot=_pot, duration=_dur)
    except Exception as exc:
        log.warning("Percival close notify failed: %s", exc)
    log.info("CLOSED %s @ $%.2f | %s | pts=%+.2f | P&L=£%+.2f | capital=£%.2f",
             trade.direction, _disp, reason, trade.points_gained, trade.pnl_gbp, account.capital_gbp)


def _maybe_notify_two_speed(trade) -> None:
    """Fire a one-time Pushover when the two-speed trail first engages (brief Part 5). Silent on
    paper (LIVE_NOTIFICATIONS gate in _send); one-shot via a flag on the trade."""
    if trade is None or getattr(trade, "_two_speed_notified", False):
        return
    best = (trade.trail_best - trade.entry_price) if trade.direction == "LONG" else (trade.entry_price - trade.trail_best)
    if best >= TIGHT_TRAIL_ACTIVATE_POINTS:
        locked_pts = (trade.stop_loss - trade.entry_price) if trade.direction == "LONG" else (trade.entry_price - trade.stop_loss)
        try:
            notify_two_speed_activated(TIGHT_TRAIL_ACTIVATE_POINTS, max(locked_pts, 0.0) * getattr(trade, "stake", 0.0), trade.stop_loss)
        except Exception as exc:
            log.warning("two-speed notify failed: %s", exc)
        trade._two_speed_notified = True


_kill_notified = False


def _maybe_notify_kill_switch(account) -> None:
    """Fire ONE kill-switch Pushover the moment it activates; re-arm when it resets (Part 2)."""
    global _kill_notified
    if getattr(account, "kill_switch_active", False):
        if not _kill_notified:
            _kill_notified = True
            try:
                notify_kill_switch_triggered(getattr(account, "kill_switch_tier", 1),
                                             getattr(account, "kill_switch_reason", ""), 0,
                                             account.daily_pnl_gbp, account.capital_gbp)
            except Exception as exc:
                log.warning("kill-switch notify failed: %s", exc)
    else:
        _kill_notified = False


# ── Position reconciliation + balance/market caches now live in camelot_engine.reconciliation
# (Step 2c). main wires GoldBase's epic / session schedule / notifier callbacks into them.


def monitor(feed, stanley, account, ig, now_utc):
    if not stanley.in_trade:
        return
    price = _get_price(feed)
    if price is None:
        return
    gbpusd = get_gbpusd_rate(ig)
    if reconciliation.reconcile_external_close(stanley, account, ig, price, gbpusd, epic=GOLD_EPIC,
                                               live_execution=_LIVE_EXECUTION, system_name="GoldBase",
                                               notify_error=notify_system_error,
                                               notify_external=notify_external_close,
                                               spread_points=SPREAD_POINTS):
        return   # position closed externally -- booked as EXTERNAL_CLOSE, now FLAT
    if should_force_close(now_utc):
        stanley.close_trade(price, "FORCE_CLOSE_2045", gbpusd)
        _on_close(stanley, account, price, "FORCE_CLOSE_2045")
        return
    reason = stanley.monitor_trade(price, gbpusd)   # trailing + ladder + stop/target
    if reason:
        _on_close(stanley, account, price, reason)
    else:
        _maybe_notify_two_speed(stanley.current_trade)


def run_candle_tick(feed, stanley, account, ig):
    now_utc = datetime.now(timezone.utc)
    period = get_liquidity_period(now_utc)
    _last["session"] = period
    try:
        feed.refresh()
    except Exception as exc:
        log.error("Data refresh failed: %s -- skipping tick", exc)
        return
    try:
        bar_1d = feed.latest_bar("1d")
    except Exception:
        bar_1d = None
    try:
        df_1d = feed.get("1d")            # full daily frame -- v1.0.1 daily filter needs recent bars
    except Exception:
        df_1d = None
    try:
        bar_1h = feed.latest_bar("1h")
        bar_5m = feed.latest_bar("5m")
    except Exception:
        log.warning("Insufficient indicator data -- skipping tick")
        return

    price = float(bar_5m["close"])
    gbpusd = get_gbpusd_rate(ig)
    _last["price"] = price

    if stanley.in_trade:
        return   # entries only when flat; exits handled by the monitor loop

    if not is_market_open(now_utc):
        _last["lancelot"] = "market closed (%s)" % period
        return

    signal_dir = ssl_agreement(bar_1d, bar_1h, bar_5m)
    _last["signal"] = signal_dir
    checks = run_all_pre_checks(bar_1h, bar_5m, account, None, df_1d,
                                proposed_direction=(signal_dir or "BOTH"), now_utc=now_utc)
    _last["checks"] = run_individual_pre_checks(bar_1h, bar_5m, account, None, df_1d,
                                                proposed_direction=(signal_dir or "BOTH"), now_utc=now_utc)
    _last["lancelot"] = "CLEAR" if checks.get("passed") else ("BLOCKED: " + str(checks.get("reason") or "--"))

    if not checks.get("passed"):
        log.info("Lancelot BLOCK: %s", checks.get("reason"))
        return
    if signal_dir is None:
        log.info("No 3-TF SSL agreement -- no trade")
        return

    mode = direction_switch.get_mode()
    exec_dir = signal_dir if mode == "WITH" else direction_switch.flip(signal_dir)
    log.info("SIGNAL %s | switch %s -> execute %s", signal_dir, mode, exec_dir)
    _open(stanley, ig, exec_dir, price, period, gbpusd)


# ── Dashboard push ────────────────────────────────────────────────────────────

# ── Market status for the dashboard dot (Part 4d) ─────────────────────────────
# in_session = the trading schedule (is_market_open, no API). tradeable = Capital.com marketStatus,
# cached 60s so the dot never hammers /markets. Dot: green = in session + tradeable; amber = in session
# but Capital temporarily closed (e.g. daily break); red = out of session. Hours are display-only.
SESSION_HOURS = "22:00–20:45 UTC"
def push_dashboard(stanley, account, mode, ig=None):
    trade = stanley.current_trade
    price = _last.get("price")
    pos, floating, locked = None, 0.0, None
    if trade is not None and stanley.in_trade:
        try:
            pts = (price - trade.entry_price) if trade.direction == "LONG" else (trade.entry_price - price)
            floating = round(pts * trade.stake, 2)
        except Exception:
            floating = 0.0
        # STAGE B: show the REAL floating P&L from Capital.com (matches the account incl. spread) when live.
        if _LIVE_EXECUTION and ig is not None:
            _live_float = live_executor.position_pnl(ig, GOLD_EPIC)
            if _live_float is not None:
                floating = _live_float
        # Locked profit = guaranteed GBP if the stop fires now (stop past entry). GoldBase v1.0.4:
        # derive from stop-vs-entry, NOT the retired ladder's ladder_floor_gbp -- the two-speed
        # trail (v1.0.3) locks via the stop itself, so the old field stayed 0 and under-reported.
        locked_pts = (trade.stop_loss - trade.entry_price) if trade.direction == "LONG" \
                     else (trade.entry_price - trade.stop_loss)
        locked = round(locked_pts * trade.stake, 2) if locked_pts > 0 else None
        pos = {"direction": trade.direction, "entry": round(trade.entry_price, 2),
               "stop": round(trade.stop_loss, 2), "target": round(trade.take_profit, 2),
               "stake": round(trade.stake, 2), "floating_gbp": floating,
               "ladder_step": getattr(trade, "ladder_step", 0), "locked_gbp": locked}
    lanc = "IN TRADE" if stanley.in_trade else _last.get("lancelot", "--")
    pot, acc_type = reconciliation.account_pot(ig)
    # Part 1 (13 Aug 2026): risk per trade is RISK_PCT of the FIXED NOTIONAL_CAPITAL (£60 on £3,000),
    # matching how the trade is actually sized. It used to be 2% of the real account balance, which
    # showed £220 against the £11,000 demo pot -- sizing never uses the real balance.
    risk = round(NOTIONAL_CAPITAL * RISK_PCT, 2)
    payload = {
        "system": "GoldBase", "version": VERSION, "port": PORT,
        "account_balance": pot, "account_type": acc_type,
        "risk_per_trade": risk, "total_pot": pot,
        "mode": mode, "session": _last.get("session", "--"),
        "market": reconciliation.market_status(ig, datetime.now(timezone.utc), GOLD_EPIC, is_market_open, SESSION_HOURS),
        "updated_utc": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "price": round(price, 2) if price is not None else None,
        "in_trade": stanley.in_trade, "position": pos,
        "floating_gbp": floating, "locked_gbp": locked,
        "signal": _last.get("signal") or "--",
        "lancelot": lanc,
        "checks": _last.get("checks", {}),
        "portfolio": {"balance": round(stanley.capital_gbp, 2),
                      "today_pnl": round(account.daily_pnl_gbp, 2),
                      "floating_gbp": floating},
    }
    try:
        requests.post(DASHBOARD_URL, json=payload, timeout=3)
    except Exception:
        pass


# ── Main loop ─────────────────────────────────────────────────────────────────

def _reconcile_and_adopt(stanley, where):
    """Rule 14: run orphan-position reconciliation at EVERY transition onto the active broker account --
    process startup AND a DEMO->LIVE account switch -- reusing reconcile_live_position() exactly as built
    and tested. Fires ONE adoption/abort notification. Idempotent + safe: genuinely flat, or paper mode,
    -> harmless no-op. Never raises."""
    try:
        res = stanley.reconcile_live_position()
    except Exception as exc:
        log.warning("reconcile/adopt (%s) failed: %s", where, exc)
        return
    if res and res.get("adopted"):
        try:
            notify_position_adopted(res["direction"], res["entry_price"], res["stop_loss"],
                                    res.get("take_profit"), res.get("stake", 0.0))
        except Exception:
            pass
    elif res and res.get("aborted"):
        try:
            notify_system_error("GoldBase: found an open broker position on %s but could NOT read its "
                                "stop/entry -- NOT adopted, engine staying flat. Manual check needed." % where)
        except Exception:
            pass


def _sync_account(ig, stanley, account=None):
    """Follow the DEMO/LIVE switch. If trading_mode changed and we are FLAT, re-point the connector at the
    selected Capital.com account (reconnects). NEVER switches accounts while a position is open -- that
    position is managed on the account it was opened on until it closes (the RoundTableBase switch is
    itself blocked while a position is open). Fail-safe: any error leaves the current account untouched.
    ACCOUNT ISOLATION (17 Aug 2026): on a successful switch, reset AccountState so the new account starts
    clean (no inherited cooldown / consecutive-loss / daily-loss-kill from the other account)."""
    try:
        from camelot_engine import trading_mode
        desired = trading_mode.read_mode()
        if ig is None or desired == ig.account_type:
            return
        if getattr(stanley, "in_trade", False):
            return
        if ig.set_account(desired):
            log.warning("ACCOUNT SWITCHED to %s (trading_mode=%s)", ig.account_type, desired)
            if account is not None:
                account.reset_for_account_switch(ig.account_type, TRADES_LOG)   # isolate: clean state per account
            # Rule 14 -- LIVE-transition adoption (20 Aug 2026): the flip onto LIVE is a transition onto broker
            # truth, exactly like startup. Re-verify so a real position already open on LIVE is ADOPTED, not
            # left flat-on-live (Rule 13 forces DEMO on every reboot, so the flip is where a pre-existing live
            # position first becomes visible). Only on the ->LIVE direction.
            if ig.account_type == "LIVE":
                _reconcile_and_adopt(stanley, "the DEMO->LIVE switch")
        else:
            log.warning("ACCOUNT SWITCH to %s refused (credentials not configured?) -- staying on %s",
                        desired, ig.account_type)
    except Exception as exc:
        log.warning("account sync error: %s", exc)


def main() -> None:
    log.info("=" * 70)
    log.info("  GoldBase A.I. v%s  (Benchmark Desk, port %d)", VERSION, PORT)
    log.info("  Gold XAU/USD | Capital.com | Pure Lancelot + 3-TF SSL + WITH/AGAINST")
    log.info("  Direction: %s | account: %s (DEMO/LIVE switch)", direction_switch.get_mode(), trading_mode.read_mode())
    log.info("=" * 70)

    ig = CapitalComConnector(trading_mode.read_mode())   # DEMO/LIVE from the switch (startup DEMO)
    ig_connected = False
    try:
        ig.connect()
        ig_connected = True
        log.info("Capital.com connected")
    except Exception as exc:
        log.error("Capital.com connect failed: %s -- yfinance fallback", exc)

    feed = GoldDataFeed(ig_connector=ig if ig_connected else None)
    try:
        feed.initialise()
    except Exception as exc:
        log.warning("Initial data load partial: %s", exc)

    stanley = PaperTraderGold()
    # ── STAGE B: attach the connector so Stanley can place/close REAL demo orders, then reconcile any
    # restored open position against Capital.com (so a restart never leaves us managing a phantom). ──
    stanley.ig = ig    # pass the connector object always; it self-heals a failed startup connect
    _reconcile_and_adopt(stanley, "startup")   # phantom guard AND orphan-position adoption (Rule 12/14)
    if _LIVE_EXECUTION:
        log.warning("*** STAGE B LIVE EXECUTION ON -- REAL demo orders will be placed on the "
                    "Capital.com %s account. ***", (ig.account_type if ig_connected else "?"))
    else:
        log.info("Stage B live execution OFF (LIVE_EXECUTION=False) -- paper simulation only.")
    account = AccountState(capital=stanley.capital_gbp)
    # Today P&L Persist Fix (30 Jul 2026) + ACCOUNT ISOLATION (17 Aug 2026): seed the in-memory daily tally
    # from today's closed trades FOR THE CURRENT ACCOUNT ONLY, so a live restart never inherits demo P&L.
    account.daily_pnl_gbp = _today_realised_pnl(TRADES_LOG, account=trading_mode.read_mode())
    if account.daily_pnl_gbp:
        log.info("Restored today's realised P&L (%s account) from trade log: GBP %.2f",
                 trading_mode.read_mode(), account.daily_pnl_gbp)
    try:
        notify_system_startup(capital=stanley.capital_gbp, mode=trading_mode.read_mode())
    except Exception:
        pass
    SHUTDOWN_FLAG.unlink(missing_ok=True)

    # Stagger Capital.com requests (shared demo account with the original desk).
    delay = 30 + random.uniform(0, 10)
    log.info("Staggering %.0fs before main loop (shared Capital.com demo)", delay)
    time.sleep(delay)

    log.info("Running. Dashboard: http://localhost:%d", PORT)
    last_candle = 0.0
    last_monitor = 0.0
    last_push = 0.0
    last_order_recon = 0.0

    while not _SHUTDOWN:
        try:
            if SHUTDOWN_FLAG.exists():
                log.info("Shutdown flag seen -- stopping (left for watchdog).")
                break
            now = time.monotonic()
            now_utc = datetime.now(timezone.utc)
            account.maybe_reset_daily(now_utc)  # Benchmark daily-reset fix
            _sync_account(ig, stanley, account)  # follow the DEMO/LIVE switch (flat-only) + isolate state
            _maybe_notify_kill_switch(account)  # one Pushover on kill-switch activation

            if check_kill_switch_reset(account):
                account.kill_switch_tier = 0

            if (now - last_monitor) >= MONITOR_SECONDS:
                try:
                    monitor(feed, stanley, account, ig, now_utc)
                except Exception as exc:
                    log.warning("monitor error: %s", exc)
                last_monitor = now

            if (now - last_candle) >= CANDLE_SECONDS:
                try:
                    run_candle_tick(feed, stanley, account, ig)
                except Exception as exc:
                    log.warning("tick error: %s", exc)
                last_candle = now

            if (now - last_push) >= 15:
                push_dashboard(stanley, account, direction_switch.get_mode(), ig)
                last_push = now

            # AUTOMATED order-audit reconcile vs Capital.com /history/activity (hourly, go-live safety)
            if _LIVE_EXECUTION and ig is not None and (now - last_order_recon) >= 3600:
                last_order_recon = now
                try:
                    _mm = live_executor.reconcile_orders(ig, GOLD_EPIC, hours=24)
                    _gate = live_executor.reconcile_alert_gate(_mm, RECON_STATE_FILE)
                    if _gate == "push":
                        for _m in _mm:
                            log.error("ORDER RECONCILE MISMATCH: %s", _m)
                        notify_system_error("GoldBase order reconcile: %d mismatch(es) vs Capital.com -- check logs/order_audit.csv." % len(_mm))
                    elif _gate == "suppress":
                        log.info("Order reconcile: %d KNOWN mismatch(es) still present -- suppressed (already alerted): %s", len(_mm), _mm)
                    elif _gate == "resolved":
                        log.info("Order reconcile: previously-flagged mismatch(es) now RESOLVED -- clean.")
                        try:
                            notify_reconcile_clear()
                        except Exception:
                            pass
                    else:
                        log.info("Order reconcile OK -- audit log matches Capital.com.")
                except Exception as _e:
                    log.warning("order reconcile failed: %s", _e)

            time.sleep(2)
        except Exception as exc:
            log.error("Main loop error: %s", exc)
            time.sleep(30)

    log.info("GoldBase stopped. Final capital: £%.2f", stanley.capital_gbp)


if __name__ == "__main__":
    main()
