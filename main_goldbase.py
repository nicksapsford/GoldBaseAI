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
from capitalcom_connector import CapitalComConnector
from notifier_gold import (
    notify_system_startup, notify_trade_opened,
    notify_trade_closed_win, notify_trade_closed_loss, notify_system_error,
    notify_two_speed_activated, notify_external_close, notify_margin_rejection,
    notify_kill_switch_triggered,
)
from paper_trader_gold import PaperTraderGold, TRADES_LOG, LIVE_EXECUTION as _LIVE_EXECUTION
import live_executor
import trading_mode
from pre_checks_gold import run_all_pre_checks, run_individual_pre_checks, check_kill_switch_reset
from strategy_gold import (should_force_close, get_gbpusd_rate, TIGHT_TRAIL_ACTIVATE_POINTS,
                           NOTIONAL_CAPITAL, RISK_PCT)
import direction_switch

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
_VER = BASE_DIR / "VERSION"
VERSION = _VER.read_text().strip() if _VER.exists() else "1.0.0"

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SHUTDOWN_FLAG = LOG_DIR / "shutdown.flag"

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


def _today_realised_pnl(csv_path, account=None) -> float:
    """Sum today's (UTC) realised P&L from the trade-log CSV. ACCOUNT ISOLATION (17 Aug 2026): when `account`
    (DEMO/LIVE) is given, count ONLY that account's trades -- so the live kill switch never inherits demo P&L
    and vice versa. Legacy rows with no `account` column are EXCLUDED from an account-filtered sum (they
    predate isolation and must not contaminate either account). Only CLOSED trades are in this CSV.
    Robust: missing file / no trades / bad rows -> 0.0. All timestamps UTC."""
    import csv as _csv
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _Path
    try:
        p = _Path(csv_path)
        if not p.exists():
            return 0.0
        today = _dt.now(_tz.utc).strftime("%Y-%m-%d")
        want = account.strip().upper() if account else None
        total = 0.0
        with p.open(newline="", encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                d = (row.get("date") or "").strip()
                if not d:
                    d = (row.get("entry_time") or "").strip()[:10]
                if d != today:
                    continue
                if want is not None and (row.get("account") or "").strip().upper() != want:
                    continue                       # only this account's trades (legacy blank-account rows excluded)
                try:
                    total += float(row.get("pnl_gbp") or 0.0)
                except (TypeError, ValueError):
                    continue
        return round(total, 2)
    except Exception:
        return 0.0


class AccountState:
    """Live account state passed to the Lancelot pre-checks (kill switch, losses)."""

    def __init__(self, capital: float) -> None:
        self.capital_gbp = capital
        self.daily_pnl_gbp = 0.0
        self.current_day = datetime.now(timezone.utc).date()  # Benchmark daily-reset fix
        self.consecutive_losses = 0
        self.last_loss_time = None
        self.kill_switch_active = False
        self.kill_switch_tier = 0
        self.kill_switch_until = None
        self.kill_switch_reason = ""
        self.kill_history = []

    def record_trade(self, pnl_gbp: float) -> None:
        self.daily_pnl_gbp = round(self.daily_pnl_gbp + pnl_gbp, 2)
        self.capital_gbp = round(self.capital_gbp + pnl_gbp, 2)
        if pnl_gbp < 0:
            self.consecutive_losses += 1
            self.last_loss_time = datetime.now(timezone.utc)
        else:
            self.consecutive_losses = 0

    def maybe_reset_daily(self, now_utc) -> bool:
        """Benchmark daily-reset fix (22 Jul 2026). On a new UTC trading day, clear the
        daily loss tally and the daily-loss kill switch so yesterday's loss does not carry
        into today. Previously daily_pnl_gbp was zeroed ONLY at process start, so a prior
        day's loss permanently re-triggered the kill switch until a manual restart."""
        today = now_utc.date()
        if today == self.current_day:
            return False
        self.current_day = today
        self.daily_pnl_gbp = 0.0
        self.consecutive_losses = 0
        self.kill_switch_active = False
        self.kill_switch_tier = 0
        self.kill_switch_until = None
        self.kill_switch_reason = ""
        log.info("New UTC trading day %s -- daily P&L + kill switch reset.", today)
        return True

    def reset_for_account_switch(self, account_label, csv_path) -> None:
        """ACCOUNT ISOLATION (17 Aug 2026). On a DEMO<->LIVE switch the two accounts must NOT share state --
        a demo loss must never block live (cooldown/consec) or trip the live kill switch, and vice versa.
        Clear cooldown / consecutive-loss / daily-loss-kill state, then reseed today's realised P&L from THIS
        account's trades only. Each account starts clean."""
        self.consecutive_losses = 0
        self.last_loss_time = None
        self.kill_switch_active = False
        self.kill_switch_tier = 0
        self.kill_switch_until = None
        self.kill_switch_reason = ""
        self.daily_pnl_gbp = _today_realised_pnl(csv_path, account=account_label)
        log.warning("ACCOUNT ISOLATION: state reset for %s -- cooldown/consec/kill cleared; today P&L reseeded "
                    "to GBP %.2f from %s trades only.", account_label, self.daily_pnl_gbp, account_label)


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
        # STAGE B: the real demo order was refused/failed -> stay FLAT, alert, do NOT retry.
        try:
            notify_margin_rejection("Order rejected -- see logs/order_audit.csv")
        except Exception:
            pass
        log.error("OPEN FAILED: real demo order not placed -- remaining FLAT.")
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
    _pot = _bal_cache.get("balance")   # last-known real Capital.com balance (cached, no extra call)
    try:
        if trade.pnl_gbp >= 0:
            notify_trade_closed_win(trade.direction, price, trade.points_gained,
                                    trade.pnl_gbp, account.capital_gbp, reason, pot=_pot, duration=_dur)
        else:
            notify_trade_closed_loss(trade.direction, price, trade.points_gained,
                                     trade.pnl_gbp, account.capital_gbp, reason, pot=_pot, duration=_dur)
    except Exception as exc:
        log.warning("Percival close notify failed: %s", exc)
    log.info("CLOSED %s @ $%.2f | %s | pts=%+.2f | P&L=£%+.2f | capital=£%.2f",
             trade.direction, price, reason, trade.points_gained, trade.pnl_gbp, account.capital_gbp)


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


# ── Periodic position reconciliation (Part 2, 11 Aug 2026) ──────────────────────────────────────
# While in a LIVE trade, verify (throttled ~60s) that the position still exists on Capital.com. If it
# was closed EXTERNALLY (margin liquidation, manual close on the app, network) before our own stop
# fired, book it as EXTERNAL_CLOSE (real P&L from the balance delta), return FLAT, and alert Nick.
# FAIL-SAFE: NEVER flatten on an uncertain API response -- only on a CONFIRMED "not found".
_recon = {"fails": 0, "last_ts": 0.0}


def _reconcile_external_close(stanley, account, ig, price, gbpusd) -> bool:
    if not (_LIVE_EXECUTION and ig is not None and stanley.in_trade):
        return False
    if not getattr(stanley.current_trade, "deal_id", None):
        return False
    now = time.time()
    if now - _recon["last_ts"] < 60:          # throttle (brief cadence: <= every poll cycle / 5 min)
        return False
    _recon["last_ts"] = now
    pos = live_executor.existing_position(ig, GOLD_EPIC)
    if pos == "UNKNOWN":                       # API failure -> NEVER flatten on uncertain
        _recon["fails"] += 1
        log.warning("reconcile: could not verify GOLD position (%d consecutive failures)", _recon["fails"])
        if _recon["fails"] == 3:
            try:
                notify_system_error("GoldBase: 3 consecutive position-reconcile failures -- check Capital.com/network.")
            except Exception:
                pass
        return False
    _recon["fails"] = 0
    if pos is None:                            # CONFIRMED closed externally
        deal_id = getattr(stanley.current_trade, "deal_id", None)
        # P&L ACCURACY (17 Aug 2026): read the ACTUAL close fill from Capital.com history rather than
        # estimating the exit from the last price feed, so the CSV matches the broker. Fail-safe: fall back
        # to the estimate if history is unavailable.
        _entry = getattr(stanley.current_trade, "entry_price", None)
        _real = live_executor.external_close_price(ig, GOLD_EPIC, entry_price=_entry)
        _exit_px = _real if _real is not None else price
        log.warning("EXTERNAL CLOSE detected: GOLD position gone on Capital.com (deal %s) -- booking at %s%s + FLAT.",
                    deal_id, _exit_px, "" if _real is not None else " (ESTIMATED -- history unavailable)")
        trade = stanley.close_trade(_exit_px, "EXTERNAL_CLOSE", gbpusd)
        if trade is not None:
            account.record_trade(trade.pnl_gbp)
        try:
            notify_external_close(trade.direction if trade else "?",
                                  trade.pnl_gbp if trade else 0.0,
                                  reason="unknown", pot=_bal_cache.get("balance"))
        except Exception:
            pass
        return True
    return False


def monitor(feed, stanley, account, ig, now_utc):
    if not stanley.in_trade:
        return
    price = _get_price(feed)
    if price is None:
        return
    gbpusd = get_gbpusd_rate(ig)
    if _reconcile_external_close(stanley, account, ig, price, gbpusd):
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

# ── Account balance (Part 3, READ-ONLY) ───────────────────────────────────────
# TOTAL POT = the real Capital.com account balance (demo now, live later). Cached 60s so we never
# hammer the /accounts endpoint. NEVER places an order -- pure read. 2% of it = risk budget per trade.
_bal_cache = {"ts": 0.0, "balance": None, "acc_type": None}


def get_account_pot(ig):
    """Return (balance_or_None, acc_type). Cached 60s PER ACCOUNT; keeps last-good on a transient
    API failure.

    Part 1 (13 Aug 2026): the cache used to be keyed on TIME ONLY, while acc_type was read live. So
    for up to 60 seconds after Nick flipped the DEMO/LIVE switch the dashboard paired the NEW account
    label with the OLD account's balance -- e.g. a red LIVE banner over the £11,000 demo pot. The
    cache is now invalidated the moment the account changes, and the stale figure is dropped
    immediately so the other _bal_cache readers (the Percival 'pot' in the close notifications)
    cannot serve it either."""
    acc_type = ig.account_type if ig is not None else "DEMO"
    now = time.time()
    if _bal_cache.get("acc_type") != acc_type:
        _bal_cache.update(ts=0.0, balance=None, acc_type=acc_type)
    if _bal_cache["balance"] is not None and (now - _bal_cache["ts"]) < 60:
        return _bal_cache["balance"], acc_type
    bal = None
    try:
        if ig is not None:
            if not ig.connected:
                ig.connect()          # self-heal a failed startup connect (engine may be on yfinance fallback)
            if ig.connected:
                bal = ig.get_account_balance()
    except Exception:
        bal = None
    if bal is not None:
        _bal_cache.update(ts=now, balance=bal, acc_type=acc_type)
    return _bal_cache["balance"], acc_type


# ── Market status for the dashboard dot (Part 4d) ─────────────────────────────
# in_session = the trading schedule (is_market_open, no API). tradeable = Capital.com marketStatus,
# cached 60s so the dot never hammers /markets. Dot: green = in session + tradeable; amber = in session
# but Capital temporarily closed (e.g. daily break); red = out of session. Hours are display-only.
SESSION_HOURS = "22:00–20:45 UTC"
_mkt_cache = {"ts": 0.0, "tradeable": None}


def get_market_status(ig, now_utc):
    in_sess = is_market_open(now_utc)
    now = time.time()
    if _mkt_cache["tradeable"] is None or (now - _mkt_cache["ts"]) >= 60:
        try:
            if ig is not None and ig.connected:
                _mkt_cache.update(ts=now, tradeable=bool(live_executor.market_tradeable(ig, GOLD_EPIC)))
        except Exception:
            pass
    return {"in_session": in_sess, "tradeable": _mkt_cache["tradeable"], "hours": SESSION_HOURS}


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
    pot, acc_type = get_account_pot(ig)
    # Part 1 (13 Aug 2026): risk per trade is RISK_PCT of the FIXED NOTIONAL_CAPITAL (£60 on £3,000),
    # matching how the trade is actually sized. It used to be 2% of the real account balance, which
    # showed £220 against the £11,000 demo pot -- sizing never uses the real balance.
    risk = round(NOTIONAL_CAPITAL * RISK_PCT, 2)
    payload = {
        "system": "GoldBase", "version": VERSION, "port": PORT,
        "account_balance": pot, "account_type": acc_type,
        "risk_per_trade": risk, "total_pot": pot,
        "mode": mode, "session": _last.get("session", "--"),
        "market": get_market_status(ig, datetime.now(timezone.utc)),
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

def _sync_account(ig, stanley, account=None):
    """Follow the DEMO/LIVE switch. If trading_mode changed and we are FLAT, re-point the connector at the
    selected Capital.com account (reconnects). NEVER switches accounts while a position is open -- that
    position is managed on the account it was opened on until it closes (the RoundTableBase switch is
    itself blocked while a position is open). Fail-safe: any error leaves the current account untouched.
    ACCOUNT ISOLATION (17 Aug 2026): on a successful switch, reset AccountState so the new account starts
    clean (no inherited cooldown / consecutive-loss / daily-loss-kill from the other account)."""
    try:
        import trading_mode
        desired = trading_mode.read_mode()
        if ig is None or desired == ig.account_type:
            return
        if getattr(stanley, "in_trade", False):
            return
        if ig.set_account(desired):
            log.warning("ACCOUNT SWITCHED to %s (trading_mode=%s)", ig.account_type, desired)
            if account is not None:
                account.reset_for_account_switch(ig.account_type, TRADES_LOG)   # isolate: clean state per account
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
    stanley.reconcile_live_position()
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
                    if _mm:
                        for _m in _mm:
                            log.error("ORDER RECONCILE MISMATCH: %s", _m)
                        notify_system_error("GoldBase order reconcile: %d mismatch(es) vs Capital.com -- check logs/order_audit.csv." % len(_mm))
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
