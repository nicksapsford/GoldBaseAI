"""
GoldTrader AI -- pre_checks_gold.py  (Lancelot)
Hard filter guardian. Runs before Arthur is ever called.
Gold-specific: 24/7 market, liquidity-period aware, Asian-session tightening.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import pandas as pd

from data_feed_gold import (
    is_market_open, get_liquidity_period,
    ASIAN, LONDON, NEW_YORK, OVERLAP, CLOSING, CLOSED,
)

log = logging.getLogger("GoldTrader.Lancelot")

# ── Thresholds ────────────────────────────────────────────────────────────────

DAILY_LOSS_LIMIT_GBP    = 60.0   # 6% of £1,000 -- hard stop for the day
MAX_CONSECUTIVE_LOSSES  = 5
COOLDOWN_MINUTES        = 30
MIN_TMO_FOR_ENTRY       = 0.3
NO_ENTRY_AFTER_MIN      = 20 * 60 + 30   # 20:30 UTC -- no new entries within 30m of close

RSI_LONG_NORMAL   = 52    # relaxed 55->52 (v1.0.6, backtest_gold validated)
RSI_SHORT_NORMAL  = 48    # relaxed 45->48 (v1.0.6, backtest_gold validated)
# Asian-session RSI tightening REMOVED 25 Jul 2026 (filter-audit tidy-up): the two live
# Gold systems dropped it on 23 Jul, so the pure-Lancelot benchmark must match -- same
# RSI bar in ALL sessions, no Asian special case. is_asian is still flagged for logging.
RSI_LONG_ASIAN    = RSI_LONG_NORMAL
RSI_SHORT_ASIAN   = RSI_SHORT_NORMAL

CHOPPY_RSI_THRESHOLD    = 5.0
CHOPPY_TMO_THRESHOLD    = 0.5
CHOPPY_SIGNALS_REQUIRED = 2


# ── Result builders ───────────────────────────────────────────────────────────

def _pass() -> dict:
    return {"passed": True, "reason": None}


def _fail(reason: str, block_direction: str = "BOTH") -> dict:
    log.info("  PRE-CHECK FAILED: %s", reason)
    return {"passed": False, "reason": reason, "block_direction": block_direction, "decision": "STAY_OUT"}


def _trigger_kill_switch(account, reason: str) -> dict:
    """Tiered kill switch. 1st trigger = 6h wait; 2nd = 12h; 3rd+ = 24h."""
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=48)
    history = [
        t for t in account.kill_history
        if datetime.fromisoformat(t).replace(tzinfo=timezone.utc) > cutoff
    ]
    history.append(now.isoformat())
    count = len(history)
    if count == 1:
        tier, wait_hours = 1, 6
    elif count == 2:
        tier, wait_hours = 2, 12
    else:
        tier, wait_hours = 3, 24
    account.kill_history       = history
    account.kill_switch_active = True
    account.kill_switch_reason = reason
    account.kill_switch_tier   = tier
    account.kill_switch_until  = (now + timedelta(hours=wait_hours)).isoformat()
    log.warning("KILL SWITCH (Tier %d) -- %s | auto-resume in %dh", tier, reason, wait_hours)
    result = _fail(reason)
    result["kill_switch_triggered"] = True
    result["kill_tier"] = tier
    return result


# ── Individual checks ─────────────────────────────────────────────────────────

def check_kill_switch(account) -> dict:
    if account.kill_switch_active:
        return _fail(f"KILL SWITCH ACTIVE -- {account.kill_switch_reason or 'triggered'}")
    return _pass()


def check_daily_loss_limit(account) -> dict:
    if account.daily_pnl_gbp <= -DAILY_LOSS_LIMIT_GBP:
        reason = (f"Daily loss limit hit (GBP {account.daily_pnl_gbp:.2f} / "
                  f"limit GBP -{DAILY_LOSS_LIMIT_GBP:.2f})")
        return _trigger_kill_switch(account, reason)
    return _pass()


def check_consecutive_losses(account) -> dict:
    n = account.consecutive_losses
    if n >= MAX_CONSECUTIVE_LOSSES:
        return _trigger_kill_switch(account, f"{n} consecutive losses")
    return _pass()


def check_kill_switch_reset(account) -> bool:
    """Auto-reset kill switch after the tier wait period. Returns True if reset."""
    if not account.kill_switch_active or not account.kill_switch_until:
        return False
    until = datetime.fromisoformat(account.kill_switch_until)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) < until:
        return False
    tier = account.kill_switch_tier
    account.kill_switch_active = False
    account.kill_switch_reason = ""
    account.kill_switch_until  = None
    account.consecutive_losses = 0
    msg = f"Kill switch reset (Tier {tier}) -- resuming"
    if tier >= 3:
        msg += ". Manual review recommended."
    log.info(msg)
    return True


def check_cooldown(account) -> dict:
    last_loss_time = account.last_loss_time
    if not last_loss_time:
        return _pass()
    try:
        last_loss = datetime.fromisoformat(last_loss_time) if isinstance(last_loss_time, str) else last_loss_time
        if last_loss.tzinfo is None:
            last_loss = last_loss.replace(tzinfo=timezone.utc)
        minutes_since = (datetime.now(timezone.utc) - last_loss).total_seconds() / 60
        if minutes_since < COOLDOWN_MINUTES:
            remaining = int(COOLDOWN_MINUTES - minutes_since)
            return _fail(f"Cooldown active -- {remaining} min remaining after last loss.")
    except Exception as exc:
        log.warning("Cooldown check error: %s", exc)
    return _pass()


def check_market_open(now_utc: Optional[datetime] = None) -> dict:
    """Block during the 21:00-22:00 UTC daily break and at weekends."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if is_market_open(now_utc):
        return _pass()
    return _fail("Gold market closed (21:00-22:00 UTC daily break or weekend) -- no entries.")


def check_near_close(now_utc: Optional[datetime] = None) -> dict:
    """No new entries after 20:30 UTC (within 30 min of the 21:00 daily close)."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    hm = now_utc.hour * 60 + now_utc.minute
    if NO_ENTRY_AFTER_MIN <= hm < 21 * 60:
        return _fail("Within 30 min of daily close (after 20:30 UTC) -- no new entries.")
    return _pass()


def check_liquidity_period(now_utc: Optional[datetime] = None) -> dict:
    """
    Soft filter -- always passes, but flags the Asian session so downstream checks
    (RSI confirmation) apply a tighter conviction threshold. Returns is_asian in
    the result dict for the runner to use.
    """
    period = get_liquidity_period(now_utc)
    is_asian = period == ASIAN
    result = _pass()
    result["liquidity_period"] = period
    result["is_asian"] = is_asian
    if is_asian:
        log.info("  Asian session -- flagged for logging only (no RSI tightening; benchmark parity 25 Jul).")
    return result


def check_daily_trend_filter(bar_1d: Optional[pd.Series], direction: str) -> dict:
    """Bidirectional daily filter (System 3 Review, 18 Jul 2026). The daily SSL sets
    the primary bias, but LONG bounce trades ARE permitted in a BEAR daily (the missed
    +94pt of 9 Jul). Rules:
      BULL daily -> LONG only  (never SHORT into a bull daily).
      BEAR daily -> SHORT (trend resumption) OR cautious LONG bounce -- the Morgan>=60
                    SHORT gate (main) + 1h/5m SSL alignment do the fine-grained gating.
      NEUTRAL / no data -> both allowed."""
    if bar_1d is None:
        return _pass()
    ssl_1d = bar_1d.get("ssl_bull")
    if pd.isna(ssl_1d):
        return _pass()
    if ssl_1d and direction == "SHORT":
        return _fail("Daily SSL is BULL -- no SHORTs into a bull daily (LONG bias today).",
                     block_direction="SHORT")
    # BEAR daily no longer blocks LONG -- cautious bounce LONGs are permitted.
    return _pass()


def check_ssl_agreement(bar_1h: pd.Series, bar_5m: pd.Series, direction: str) -> dict:
    """1h AND 5m SSL must BOTH agree with the proposed direction (System 3 Review,
    18 Jul 2026). Requiring the 5m to agree prevents shorting into a 5m bounce -- the
    Asian-session failure mode behind the ARTHUR_EXIT losses."""
    ssl_1h = bar_1h.get("ssl_bull")
    ssl_5m = bar_5m.get("ssl_bull")
    if pd.isna(ssl_1h):
        return _fail("1h SSL data unavailable")
    if direction == "LONG" and not ssl_1h:
        return _fail("1h SSL is BEAR but direction is LONG -- 1h trend disagrees.", block_direction="LONG")
    if direction == "SHORT" and ssl_1h:
        return _fail("1h SSL is BULL but direction is SHORT -- 1h trend disagrees.", block_direction="SHORT")
    if pd.notna(ssl_5m):
        if direction == "LONG" and not ssl_5m:
            return _fail("5m SSL is BEAR but direction is LONG -- entry timeframe disagrees.",
                         block_direction="LONG")
        if direction == "SHORT" and ssl_5m:
            return _fail("5m SSL is BULL but direction is SHORT -- 5m is bouncing, no short.",
                         block_direction="SHORT")
    return _pass()


def check_1h_rsi_confirms(bar_1h: pd.Series, direction: str, is_asian: bool = False) -> dict:
    """1h RSI above 52 for LONG / below 48 for SHORT (60/40 during Asian session)."""
    rsi_1h = bar_1h.get("rsi")
    if pd.isna(rsi_1h):
        return _pass()
    long_min  = RSI_LONG_ASIAN  if is_asian else RSI_LONG_NORMAL
    short_max = RSI_SHORT_ASIAN if is_asian else RSI_SHORT_NORMAL
    if direction == "LONG" and rsi_1h < long_min:
        return _fail(f"1h RSI is {rsi_1h:.1f} -- need above {long_min} for LONG"
                     f"{' (Asian session, tighter)' if is_asian else ''}.", block_direction="LONG")
    if direction == "SHORT" and rsi_1h > short_max:
        return _fail(f"1h RSI is {rsi_1h:.1f} -- need below {short_max} for SHORT"
                     f"{' (Asian session, tighter)' if is_asian else ''}.", block_direction="SHORT")
    return _pass()


def check_5m_tmo_momentum(bar_1h: pd.Series, bar_5m: pd.Series) -> dict:
    """5m TMO must show momentum: > +0.3 for LONG, < -0.3 for SHORT."""
    ssl_bull = bar_1h.get("ssl_bull")
    tmo_5m   = bar_5m.get("tmo_main")
    if pd.isna(ssl_bull) or pd.isna(tmo_5m):
        return _pass()
    if ssl_bull and tmo_5m < MIN_TMO_FOR_ENTRY:
        return _fail(f"Bullish setup but 5m TMO only {tmo_5m:.3f} -- need >{MIN_TMO_FOR_ENTRY}.",
                     block_direction="LONG")
    if not ssl_bull and tmo_5m > -MIN_TMO_FOR_ENTRY:
        return _fail(f"Bearish setup but 5m TMO only {tmo_5m:.3f} -- need <-{MIN_TMO_FOR_ENTRY}.",
                     block_direction="SHORT")
    return _pass()


def check_choppy_market(bar_1h: pd.Series, bar_5m: pd.Series) -> dict:
    """Block if RSI and TMO both near zero -- directionless."""
    choppy = []
    rsi_5m = bar_5m.get("rsi")
    if pd.notna(rsi_5m) and abs(rsi_5m - 50) <= CHOPPY_RSI_THRESHOLD:
        choppy.append(f"5m RSI near 50 ({rsi_5m:.1f})")
    tmo_5m = bar_5m.get("tmo_main")
    if pd.notna(tmo_5m) and abs(tmo_5m) <= CHOPPY_TMO_THRESHOLD:
        choppy.append(f"5m TMO near zero ({tmo_5m:.3f})")
    rsi_1h = bar_1h.get("rsi")
    if pd.notna(rsi_1h) and abs(rsi_1h - 50) <= CHOPPY_RSI_THRESHOLD:
        choppy.append(f"1h RSI near 50 ({rsi_1h:.1f})")
    if len(choppy) >= CHOPPY_SIGNALS_REQUIRED:
        return _fail(f"Choppy market: {', '.join(choppy)}. No clear direction -- best trade is no trade.")
    return _pass()


def check_candle_confirmed(bar_1h: pd.Series, bar_5m: pd.Series) -> dict:
    """Last 5m candle must be green for LONG, red for SHORT."""
    ssl_bull    = bar_1h.get("ssl_bull")
    open_price  = bar_5m.get("open")
    close_price = bar_5m.get("close")
    if pd.isna(ssl_bull) or pd.isna(open_price) or pd.isna(close_price):
        return _pass()
    candle_green = close_price >= open_price
    if ssl_bull and not candle_green:
        return _fail("Bullish setup but last 5m candle is RED -- waiting for green confirmation.",
                     block_direction="LONG")
    if not ssl_bull and candle_green:
        return _fail("Bearish setup but last 5m candle is GREEN -- waiting for red confirmation.",
                     block_direction="SHORT")
    return _pass()


# ── Master runner ─────────────────────────────────────────────────────────────

def _derive_direction(bar_1h: pd.Series, proposed_direction: str) -> str:
    if proposed_direction in ("LONG", "SHORT"):
        return proposed_direction
    return "LONG" if bar_1h.get("ssl_bull") else "SHORT"


def run_all_pre_checks(
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    account,
    current_trade=None,
    bar_1d: Optional[pd.Series] = None,
    proposed_direction: str = "BOTH",
    now_utc: Optional[datetime] = None,
) -> dict:
    """Run all pre-checks in order, returning on first failure. Arthur is called only if passed."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    log.info("--- Lancelot running pre-checks ---")

    safety_checks = [
        ("Kill switch",         lambda: check_kill_switch(account)),
        ("Daily loss limit",    lambda: check_daily_loss_limit(account)),
        ("Consecutive losses",  lambda: check_consecutive_losses(account)),
        ("Cooldown period",     lambda: check_cooldown(account)),
        ("Market open",         lambda: check_market_open(now_utc)),
        ("Not near close",      lambda: check_near_close(now_utc)),
    ]
    for name, fn in safety_checks:
        result = fn()
        if not result["passed"]:
            log.info("  [FAIL] %s -- %s", name, result["reason"])
            return result
        log.info("  [PASS] %s", name)

    # Liquidity soft filter -- sets the Asian tightening flag.
    liq = check_liquidity_period(now_utc)
    is_asian = liq.get("is_asian", False)
    log.info("  [PASS] Liquidity period (%s)", liq.get("liquidity_period"))

    if current_trade is None:
        direction = _derive_direction(bar_1h, proposed_direction)
        quality_checks = [
            ("Daily trend filter", lambda: check_daily_trend_filter(bar_1d, direction)),
            ("SSL agreement 1h+5m", lambda: check_ssl_agreement(bar_1h, bar_5m, direction)),
            ("1h RSI confirming",  lambda: check_1h_rsi_confirms(bar_1h, direction, is_asian)),
            ("5m TMO momentum",    lambda: check_5m_tmo_momentum(bar_1h, bar_5m)),
            ("Not choppy",         lambda: check_choppy_market(bar_1h, bar_5m)),
            ("Candle confirmed",   lambda: check_candle_confirmed(bar_1h, bar_5m)),
        ]
        for name, fn in quality_checks:
            result = fn()
            if not result["passed"]:
                log.info("  [FAIL] %s -- %s", name, result["reason"])
                return result
            log.info("  [PASS] %s", name)

    log.info("  All pre-checks passed -- ready for Arthur")
    return _pass()


def run_individual_pre_checks(
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    account,
    current_trade=None,
    bar_1d: Optional[pd.Series] = None,
    proposed_direction: str = "BOTH",
    now_utc: Optional[datetime] = None,
) -> dict:
    """Run each check individually for dashboard display. Returns dict of name -> bool/None."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    direction = _derive_direction(bar_1h, proposed_direction)
    is_asian  = get_liquidity_period(now_utc) == ASIAN
    checks = {}
    checks["Kill Switch OK"]        = check_kill_switch(account)["passed"]
    checks["Daily Loss OK"]         = check_daily_loss_limit(account)["passed"]
    checks["Consecutive Losses OK"] = check_consecutive_losses(account)["passed"]
    checks["Cooldown OK"]           = check_cooldown(account)["passed"]
    checks["Market Open"]           = check_market_open(now_utc)["passed"]
    checks["Not Near Close"]        = check_near_close(now_utc)["passed"]
    if current_trade is None:
        checks["Daily Trend OK"]    = check_daily_trend_filter(bar_1d, direction)["passed"]
        checks["SSL Aligned 1h+5m"] = check_ssl_agreement(bar_1h, bar_5m, direction)["passed"]
        checks["1h RSI Confirming"] = check_1h_rsi_confirms(bar_1h, direction, is_asian)["passed"]
        checks["Momentum Strong"]   = check_5m_tmo_momentum(bar_1h, bar_5m)["passed"]
        checks["Not Choppy"]        = check_choppy_market(bar_1h, bar_5m)["passed"]
        checks["Candle Confirmed"]  = check_candle_confirmed(bar_1h, bar_5m)["passed"]
    else:
        for k in ["Daily Trend OK", "1h SSL Aligned", "1h RSI Confirming",
                  "Momentum Strong", "Not Choppy", "Candle Confirmed"]:
            checks[k] = None
    return checks


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    import types
    log.info("Lancelot self-test (Gold)")
    account_ok = types.SimpleNamespace(
        kill_switch_active=False, kill_switch_reason="", kill_switch_tier=0,
        kill_switch_until=None, kill_history=[], daily_pnl_gbp=-5.0,
        consecutive_losses=1, last_loss_time=None,
    )
    bar_1h = pd.Series({"ssl_bull": True, "rsi": 62.0, "tmo_main": 2.1, "open": 4150.0, "close": 4160.0})
    bar_5m = pd.Series({"ssl_bull": True, "rsi": 58.0, "tmo_main": 0.8, "open": 4158.0, "close": 4162.0})
    result = run_all_pre_checks(bar_1h, bar_5m, account_ok, bar_1d=pd.Series({"ssl_bull": True}),
                                proposed_direction="LONG")
    log.info("Result: %s", "PASSED" if result["passed"] else f"FAILED -- {result['reason']}")
    log.info("Lancelot self-test complete.")
