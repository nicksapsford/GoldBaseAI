"""
AlbionTrader AI -- capitalcom_connector.py  (Excalibur)
Capital.com API interface via raw REST calls (requests).
Handles authentication, price fetching, and order placement.

Replaces ig_connector.py -- IG Markets API access was suspended.
"""

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

log = logging.getLogger("GoldTrader.Excalibur")

ENV_PATH = Path(__file__).parent / ".env"
if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
else:
    load_dotenv()

FTSE_EPIC       = "UK100"   # Capital.com epic for the FTSE 100 (verified against demo API 2026-07-06)
PRICE_CACHE_S   = 60
MAX_RETRIES     = 3
RETRY_DELAY_S   = 5
SESSION_MAX_AGE_S = 9 * 60   # refresh before the 10-minute expiry

DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com/api/v1"
LIVE_BASE_URL = "https://api-capital.backend-capital.com/api/v1"

# ── Live/demo price scaling (Part 2, Archie brief 13 Aug 2026) ────────────────
# Capital.com's LIVE host quotes SOME epics on a different scale to DEMO. Verified side by side
# 13 Aug 2026 on the same login:
#     OIL_BRENT   demo    86.866  ->  live  8686.1   (100x)
#     GBPUSD      demo   1.35006  ->  live 13500.8   (10,000x)
#     GOLD/US500  identical on both hosts.
# snapshot.scalingFactor reports 1 on BOTH hosts and does NOT flag this. The only signal is
# snapshot.decimalPlacesFactor, which drops on live for exactly the affected epics.
#
# We store the DEMO baseline (the instrument's true precision) as constants and DERIVE the divisor at
# runtime from the live decimalPlacesFactor -- never a hardcoded scale factor. If Capital.com changes
# the scaling, decimalPlacesFactor moves with it and we follow, instead of silently applying a stale
# correction. Unknown epic or missing field -> divisor 1.0 (no correction) + the Part 1 guard still
# has to pass, so an unhandled epic fails closed rather than trading on a mis-scaled price.
DEMO_DECIMAL_PLACES = {"GOLD": 2, "US500": 1, "OIL_BRENT": 3, "GBPUSD": 5}

# ── Fail-closed sanity guard (Part 1) -- HARD CONTROL, standing rule 7 ────────
# Stays in permanently, even once normalisation is proven. Runs AFTER normalisation as the backstop.
GBPUSD_SANE_MIN = 0.5
GBPUSD_SANE_MAX = 5.0
PRICE_REF_MAX_RATIO = 3.0        # normalised price must sit within [ref/3, ref*3]

_PUSHOVER_API = "https://api.pushover.net/1/messages.json"


def percival_price_alert(title: str, message: str) -> None:
    """Percival alert for a failed price sanity check. Self-contained (no notifier import) so this
    block stays vendored byte-identical across GoldBase/OilBase/USBase. Gated by LIVE_NOTIFICATIONS
    + creds; never raises -- a dead notifier must never block the refusal it is reporting."""
    if os.getenv("LIVE_NOTIFICATIONS", "False").strip().lower() not in ("1", "true", "yes", "on"):
        return
    try:                                  # STANDING RULE: Pushover ONLY in LIVE mode (K1); DEMO/unknown = silent
        import trading_mode as _tm
        if _tm.read_mode() != "LIVE":
            return
    except Exception:
        return
    user, token = os.getenv("PUSHOVER_USER_KEY", ""), os.getenv("PUSHOVER_API_TOKEN", "")
    if not user or not token:
        return
    try:
        mode = "LIVE"
        try:
            import trading_mode
            mode = trading_mode.read_mode()
        except Exception:
            pass
        requests.post(_PUSHOVER_API, data={
            "token": token, "user": user,
            "title": "[%s] %s" % (mode, title),
            "message": message,
            "priority": 1,          # High -- a refused order needs to be seen
        }, timeout=8)
    except Exception:
        pass


class CapitalComConnector:
    """
    Excalibur -- Capital.com API connector.

    Usage:
        cap = CapitalComConnector()
        if cap.connect():
            price = cap.get_price("FTSE")
    """

    def __init__(self, account="DEMO") -> None:
        self._connected           = False
        self._price_cache: dict   = {}   # epic -> (price_dict, timestamp)
        self._scale_cache: dict   = {}   # epic -> divisor, for the ACTIVE account (cleared on switch)

        self._cst                 = None
        self._security_token      = None
        self._session_created_at  = None   # time.monotonic() at last (re)auth

        # Shared login e-mail; per-account API key + password (+ optional account id) selected by mode.
        # DEMO -> demo host, LIVE -> live host. The active account is driven by trading_mode (DEMO/LIVE),
        # NOT a fixed .env flag -- set_account() re-points and forces a fresh session.
        self._email = os.getenv("CAPITALCOM_EMAIL", "")
        self._creds = {
            "DEMO": {"key": os.getenv("CAPITALCOM_DEMO_KEY", ""),
                     "password": os.getenv("CAPITALCOM_DEMO_PASSWORD", ""),
                     "acc_id": os.getenv("CAPITALCOM_DEMO_ACCOUNT_ID", ""),
                     "base_url": DEMO_BASE_URL},
            "LIVE": {"key": os.getenv("CAPITALCOM_LIVE_KEY", ""),
                     "password": os.getenv("CAPITALCOM_LIVE_PASSWORD", ""),
                     "acc_id": os.getenv("CAPITALCOM_LIVE_ACCOUNT_ID", ""),
                     "base_url": LIVE_BASE_URL},
        }
        self._account = "LIVE" if str(account).strip().upper() == "LIVE" else "DEMO"
        self._apply_account()

    def _apply_account(self) -> None:
        """Point the active credentials/base-url at the currently selected account."""
        c = self._creds[self._account]
        self._api_key  = c["key"]
        self._password = c["password"]
        self._acc_id   = c["acc_id"]
        self._base_url = c["base_url"]
        # Scaling is per-HOST -- DEMO and LIVE quote differently, so never carry a divisor across a
        # switch. Prices are cached too and must not survive an account change.
        self._scale_cache = {}
        self._price_cache = {}
        if not all([self._email, self._api_key, self._password]):
            log.warning("Capital.com %s credentials not fully configured in .env", self._account)

    # ── Price scaling (Part 2) + sanity guard (Part 1) ────────────────────────

    def _learn_scale(self, epic: str, snapshot: dict) -> float:
        """Derive and cache this epic's divisor from a /markets snapshot. Returns the divisor.

        divisor = 10 ** (demo_baseline_dp - live_dp), so:
            OIL_BRENT on live -> 10 ** (3 - 1) = 100     (8686.1 / 100  = 86.861)
            GBPUSD    on live -> 10 ** (5 - 1) = 10000   (13500.8/10000 = 1.35008)
            GOLD/US500        -> 10 ** 0       = 1       (unchanged)
        On DEMO every epic already reports its true precision, so every divisor is 1."""
        base = DEMO_DECIMAL_PLACES.get(epic)
        dp = (snapshot or {}).get("decimalPlacesFactor")
        if base is None or dp is None:
            self._scale_cache[epic] = 1.0
            return 1.0
        try:
            divisor = float(10 ** (int(base) - int(dp)))
        except (TypeError, ValueError):
            divisor = 1.0
        if divisor <= 0:
            divisor = 1.0
        prev = self._scale_cache.get(epic)
        self._scale_cache[epic] = divisor
        if divisor != 1.0 and prev != divisor:
            log.warning("%s price scaling detected on %s: decimalPlacesFactor=%s vs demo baseline %s "
                        "-> dividing quotes by %g", epic, self._account, dp, base, divisor)
        return divisor

    def price_divisor(self, epic: str) -> float:
        """Cached divisor for `epic` on the ACTIVE account, fetching /markets once if not yet known.
        Returns 1.0 (no correction) if it cannot be determined."""
        if epic in self._scale_cache:
            return self._scale_cache[epic]
        try:
            resp = requests.get(f"{self._base_url}/markets/{epic}", headers=self._headers(), timeout=10)
            resp.raise_for_status()
            return self._learn_scale(epic, resp.json().get("snapshot", {}))
        except Exception as exc:
            log.warning("Could not read decimalPlacesFactor for %s: %s -- assuming no scaling.", epic, exc)
            self._scale_cache[epic] = 1.0
            return 1.0

    def normalise_price(self, epic: str, raw):
        """Broker quote -> TRUE price. Use for every price READ from Capital.com."""
        try:
            return float(raw) / self.price_divisor(epic)
        except (TypeError, ValueError, ZeroDivisionError):
            return raw

    def to_broker_price(self, epic: str, price):
        """TRUE price -> broker quote. Use for any absolute level SENT to Capital.com (e.g. stopLevel).
        Without this a Brent stop level would be sent 100x too low and rejected or filled instantly."""
        try:
            return float(price) * self.price_divisor(epic)
        except (TypeError, ValueError):
            return price

    def to_broker_distance(self, epic: str, distance):
        """TRUE point distance -> broker point distance (e.g. stopDistance). Brent's 1.5pt stop must be
        sent as 150 on a 100x-scaled live quote, otherwise the stop is 100x too tight and the trade is
        stopped out within seconds."""
        try:
            return float(distance) * self.price_divisor(epic)
        except (TypeError, ValueError):
            return distance

    def to_broker_size(self, epic: str, size):
        """TRUE position size -> broker size. The INVERSE of the price scaling.

        Confirmed 13 Aug 2026 from dealingRules.minDealSize, which moves with the quote scale:
            OIL_BRENT   demo minDealSize 1     ->  live 0.01   (ratio 100, same as the price ratio)
            GOLD/US500  demo 0.01             ->  live 0.01   (ratio 1, unscaled)
        A 100x-larger quote means each size unit carries 100x the economic exposure, so the same
        position is size/100 on live:
            demo  40    x 86.54 = 3461 exposure
            live   0.4  x 8654  = 3461 exposure   <- identical
        The scaling cancels against to_broker_distance(), so risk is preserved exactly:
            demo  40   x 1.5pt  = $60 risk
            live   0.4 x 150pt  = $60 risk
        WITHOUT this, a live Brent order would carry 100x the intended exposure and 100x the risk."""
        try:
            return float(size) / self.price_divisor(epic)
        except (TypeError, ValueError, ZeroDivisionError):
            return size

    def price_sane(self, epic: str, price, reference=None) -> bool:
        """HARD CONTROL (Part 1, standing rule 7). True when a NORMALISED price is plausible.

        Runs AFTER normalisation as the final backstop: if normalisation is wrong, missing, or the epic
        is unknown, this still refuses the trade. Two checks:
          * GBPUSD must sit inside [0.5, 5.0].
          * Any epic with a reference price (the yfinance figure the data feed already pulls) must sit
            within [ref/3, ref*3].
        Fails CLOSED -- a price it cannot evaluate as sane is not sane."""
        try:
            p = float(price)
        except (TypeError, ValueError):
            log.error("PRICE SANITY: %s price %r is not a number -- refusing.", epic, price)
            return False
        if p <= 0:
            log.error("PRICE SANITY: %s price %s is not positive -- refusing.", epic, p)
            return False
        if epic == "GBPUSD" and not (GBPUSD_SANE_MIN <= p <= GBPUSD_SANE_MAX):
            msg = ("GBPUSD implausible: %s (outside %s-%s). No orders placed. "
                   "Check the Capital.com connection." % (p, GBPUSD_SANE_MIN, GBPUSD_SANE_MAX))
            log.error("PRICE SANITY: %s -- refusing trade", msg)
            percival_price_alert("Price sanity check failed", msg)
            return False
        if reference is not None:
            try:
                ref = float(reference)
            except (TypeError, ValueError):
                ref = 0.0
            if ref > 0:
                hi, lo = ref * PRICE_REF_MAX_RATIO, ref / PRICE_REF_MAX_RATIO
                if not (lo <= p <= hi):
                    msg = ("%s price implausible: live %s vs reference %s (allowed %.4f-%.4f). "
                           "No orders placed." % (epic, p, ref, lo, hi))
                    log.error("PRICE SANITY: %s -- refusing trade", msg)
                    percival_price_alert("Price sanity check failed", msg)
                    return False
        return True

    def _ensure_account_id(self, current_id=None) -> None:
        """Pin this session to CAPITALCOM_{DEMO,LIVE}_ACCOUNT_ID via PUT /session (Part 2b, 13 Aug 2026).

        A Capital.com LOGIN can hold SEVERAL accounts (the AlbionBase demo login holds 'GBP' and
        'Account 2', one per machine). /session alone lands on whichever account carries the
        'preferred' flag, so without this the Dell and the K1 would trade the SAME account while both
        .env files claimed otherwise. The account id was previously read from .env and then never used.

        No id configured -> no-op (keeps the old preferred-account behaviour).
        Switch fails -> FAIL CLOSED: we may be sitting on the wrong account, so drop the connection
        rather than trade it."""
        want = (self._acc_id or "").strip()
        if not want:
            log.warning("No CAPITALCOM_%s_ACCOUNT_ID set -- using the login's 'preferred' account. "
                        "If this login has more than one account this may be the WRONG one.",
                        self._account)
            return
        if str(current_id or "").strip() == want:
            log.info("Capital.com session already on %s account %s", self._account, want)
            return
        try:
            resp = requests.put(
                f"{self._base_url}/session",
                headers=self._headers(),
                json={"accountId": want},
                timeout=10,
            )
            resp.raise_for_status()
            log.info("Capital.com session switched to %s account %s", self._account, want)
        except Exception as exc:
            body = ""
            try:
                body = exc.response.text if getattr(exc, "response", None) is not None else ""
            except Exception:
                body = ""
            log.error("Could not switch to %s account %s: %s %s -- treating as NOT connected "
                      "(refusing to trade an unverified account).", self._account, want, exc, body)
            self._connected = False
            self._cst = None
            self._security_token = None
            self._session_created_at = None

    def has_credentials(self, account) -> bool:
        """True when the given account (DEMO/LIVE) has a login e-mail + API key + password configured."""
        acct = "LIVE" if str(account).strip().upper() == "LIVE" else "DEMO"
        c = self._creds[acct]
        return bool(self._email and c["key"] and c["password"])

    def set_account(self, account) -> bool:
        """Switch the ACTIVE Capital.com account (DEMO<->LIVE). Drops any session so the next connect()
        authenticates against the new account; returns True on a live re-connect. No-op True if already on
        that account and connected. Returns False if the requested account has no credentials (fail safe)."""
        acct = "LIVE" if str(account).strip().upper() == "LIVE" else "DEMO"
        if acct == self._account and self._connected:
            return True
        if not self.has_credentials(acct):
            log.warning("Refusing to switch to %s -- credentials not configured", acct)
            return False
        self._account = acct
        self._apply_account()
        self._connected = False
        self._cst = None
        self._security_token = None
        self._session_created_at = None
        return self.connect()

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Authenticate with Capital.com. Returns True on success."""
        email    = self._email.strip()
        password = self._password.strip()
        api_key  = self._api_key.strip()

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    f"{self._base_url}/session",
                    headers={
                        "X-CAP-API-KEY": api_key,
                        "Content-Type":  "application/json",
                    },
                    json={
                        "identifier":        email,
                        "password":          password,
                        "encryptedPassword": False,
                    },
                    timeout=10,
                )
                resp.raise_for_status()

                cst      = resp.headers.get("CST")
                sec_tok  = resp.headers.get("X-SECURITY-TOKEN")
                if not cst or not sec_tok:
                    raise ValueError("Session response missing CST/X-SECURITY-TOKEN headers")

                self._cst                = cst
                self._security_token     = sec_tok
                self._session_created_at = time.monotonic()
                self._connected          = True

                try:
                    _current = (resp.json() or {}).get("currentAccountId")
                except Exception:
                    _current = None
                self._ensure_account_id(_current)
                if not self._connected:
                    return False

                log.info("Excalibur connected to Capital.com (%s account)", self._account)
                return True
            except Exception as exc:
                log.warning(
                    "Capital.com connection attempt %d/%d failed: %s",
                    attempt, MAX_RETRIES, exc,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_S)

        log.error("Could not connect to Capital.com after %d attempts", MAX_RETRIES)
        self._connected = False
        return False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def account_type(self) -> str:
        """The ACTIVE account (DEMO/LIVE) this connector is pointed at -- drives the dashboard account tag."""
        return self._account

    def _refresh_session(self) -> None:
        """Re-authenticate if the session is missing or older than SESSION_MAX_AGE_S."""
        if self._session_created_at is None:
            self.connect()
            return

        elapsed = time.monotonic() - self._session_created_at
        if elapsed > SESSION_MAX_AGE_S:
            log.info(
                "Capital.com session is %.0fs old -- refreshing (10-min expiry)",
                elapsed,
            )
            self.connect()

    def _headers(self) -> dict:
        return {
            "CST":              self._cst,
            "X-SECURITY-TOKEN":  self._security_token,
            "X-CAP-API-KEY":     self._api_key.strip(),
            "Content-Type":      "application/json",
        }

    # ── Price ────────────────────────────────────────────────────────────────

    def get_price(self, epic: str = FTSE_EPIC) -> Optional[dict]:
        """
        Fetch current bid/ask for an epic.
        Results are cached for PRICE_CACHE_S seconds to avoid API hammering.
        Returns: {"bid": float, "ask": float, "mid": float, "epic": str}
        """
        self._refresh_session()

        now = time.monotonic()
        cached_entry = self._price_cache.get(epic)
        if cached_entry:
            price_dict, cached_at = cached_entry
            if now - cached_at < PRICE_CACHE_S:
                return price_dict

        if not self._connected:
            log.warning("get_price called but not connected to Capital.com")
            return None

        try:
            resp = requests.get(
                f"{self._base_url}/markets/{epic}",
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            snap = resp.json().get("snapshot", {})
            # Part 2: learn this epic's scaling from the SAME response (no extra API call), then
            # normalise. On DEMO the divisor is 1 and these are no-ops.
            divisor = self._learn_scale(epic, snap)
            bid  = float(snap.get("bid", 0)) / divisor
            ask  = float(snap.get("offer", 0)) / divisor
            # Round the mid to the instrument's TRUE precision. The old fixed 1dp was fine for gold but
            # destroyed GBPUSD (1.35008 -> 1.4), which is why strategy_gold recomputes its own mid.
            _dp = DEMO_DECIMAL_PLACES.get(epic, 2)
            mid  = round((bid + ask) / 2, _dp)
            result = {"bid": bid, "ask": ask, "mid": mid, "epic": epic}
            self._price_cache[epic] = (result, now)
            log.debug("Price %s: bid=%.1f ask=%.1f", epic, bid, ask)
            return result
        except Exception as exc:
            log.error("get_price failed for %s: %s", epic, exc)
            return None

    # ── Historical prices ────────────────────────────────────────────────────

    def get_historical_prices(
        self,
        epic: str = FTSE_EPIC,
        resolution: str = "MINUTE_5",
        num_points: int = 200,
    ) -> Optional[object]:
        """
        Fetch OHLCV candles from Capital.com.
        resolution: MINUTE, MINUTE_5, MINUTE_15, HOUR, DAY
        Returns pandas DataFrame or None on failure.
        """
        self._refresh_session()

        if not self._connected:
            log.warning("get_historical_prices called but not connected")
            return None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.get(
                    f"{self._base_url}/prices/{epic}",
                    headers=self._headers(),
                    params={"resolution": resolution, "max": num_points},
                    timeout=15,
                )
                resp.raise_for_status()
                prices = resp.json().get("prices")
                if prices is None or len(prices) == 0:
                    log.warning("No prices returned for %s %s", epic, resolution)
                    return None

                import pandas as pd
                # Part 2: candles come back on the same scale as the quotes, so normalise them too --
                # otherwise a live Brent candle series is 100x the real price. No-op on demo.
                _div = self.price_divisor(epic)
                rows = []
                for p in prices:
                    snap_time = p.get("snapshotTime", "")
                    o_mid = (float(p["openPrice"]["bid"]) + float(p["openPrice"]["ask"])) / 2 / _div
                    h_mid = (float(p["highPrice"]["bid"]) + float(p["highPrice"]["ask"])) / 2 / _div
                    l_mid = (float(p["lowPrice"]["bid"]) + float(p["lowPrice"]["ask"])) / 2 / _div
                    c_mid = (float(p["closePrice"]["bid"]) + float(p["closePrice"]["ask"])) / 2 / _div
                    vol   = float(p.get("lastTradedVolume", 0) or 0)
                    rows.append({
                        "timestamp": snap_time,
                        "open":   o_mid,
                        "high":   h_mid,
                        "low":    l_mid,
                        "close":  c_mid,
                        "volume": vol,
                    })

                df = pd.DataFrame(rows)
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
                df = df.dropna(subset=["timestamp"])
                df = df.set_index("timestamp").sort_index()
                log.info(
                    "Historical prices: %s %s -- %d candles",
                    epic, resolution, len(df),
                )
                return df

            except Exception as exc:
                log.warning(
                    "Historical price attempt %d/%d failed (%s %s): %s",
                    attempt, MAX_RETRIES, epic, resolution, exc,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_S)

        log.error("Failed to fetch historical prices for %s %s", epic, resolution)
        return None

    # ── Trade execution ───────────────────────────────────────────────────────

    def open_position(
        self,
        epic: str = FTSE_EPIC,
        direction: str = "BUY",
        size: float = 0.25,
        stop_distance: float = 40.0,
        take_profit: float = None,
    ) -> Optional[dict]:
        """
        Open a position on Capital.com.
        direction: "BUY" (long) or "SELL" (short)
        size: position size (e.g. 0.50)
        stop_distance: trailing stop in points (e.g. 40)
        Returns: {"deal_reference": str, "deal_id": str} or None
        """
        self._refresh_session()

        if not self._connected:
            log.error("open_position called but not connected")
            return None

        # Part 2: the stop distance is in TRUE points; the broker wants it in ITS quote units. On a
        # 100x-scaled live Brent a 1.5pt stop must be sent as 150, otherwise it is 100x too tight and
        # the position is stopped out within seconds. No-op on demo (divisor 1).
        broker_stop = self.to_broker_distance(epic, stop_distance)
        broker_size = self.to_broker_size(epic, size)
        # Part 2 (broker-side TP): send profitDistance so the TP survives an engine crash (was hardcoded None).
        broker_tp = self.to_broker_distance(epic, take_profit) if take_profit else None
        if broker_stop != stop_distance or broker_size != size:
            log.warning("SCALED for %s on %s: size %.4f -> %.4f | stop %.4f pt -> %.4f pt "
                        "(divisor %g) -- exposure and risk are unchanged.",
                        epic, self._account, float(size), float(broker_size),
                        float(stop_distance), float(broker_stop), self.price_divisor(epic))
        try:
            resp = requests.post(
                f"{self._base_url}/positions",
                headers=self._headers(),
                json={
                    "direction":       direction,
                    "epic":            epic,
                    "size":            broker_size,
                    "guaranteedStop":  False,
                    "stopDistance":    broker_stop,
                    "stopLevel":       None,
                    "profitDistance":  broker_tp,
                    "profitLevel":     None,
                },
                timeout=10,
            )
            resp.raise_for_status()
            deal_ref = resp.json().get("dealReference", "")
            log.info(
                "Position opened | %s %s | size=%.2f | stop=%dpt | ref=%s",
                direction, epic, size, stop_distance, deal_ref,
            )
            confirm = self._confirm_deal(deal_ref)
            # Capital.com returns a dealId even for a REJECTED order -- MUST check dealStatus, else the
            # caller treats a rejected order as an open position (phantom). ACCEPTED = real fill.
            if not confirm or confirm.get("dealStatus") != "ACCEPTED":
                log.error("open_position REJECTED | epic=%s | dealStatus=%s | reason=%s | status=%s",
                          epic, (confirm or {}).get("dealStatus"), (confirm or {}).get("reason"),
                          (confirm or {}).get("status"))
                return None
            deal_id = confirm.get("dealId", "")
            if not deal_id:
                log.error("open_position: ACCEPTED but no dealId (%s) -- treating as failed.", epic)
                return None
            return {"deal_reference": deal_ref, "deal_id": deal_id}
        except Exception as exc:
            _body = ""
            try:
                _body = exc.response.text if getattr(exc, "response", None) is not None else ""
            except Exception:
                _body = ""
            log.error("open_position failed: %s %s", exc, _body)
            return None

    def close_position(
        self,
        deal_id: str,
        direction: str = "",
        size: float = 0.0,
        epic: str = "",
    ) -> bool:
        """
        Close an open position. True if it is closed, or was already gone.

        RESOLVES THE REAL dealId ITSELF -- never trusts the id it is handed (hardened 13 Aug 2026
        after a live incident). open_position() returns a deal CONFIRMATION id which is NOT the id of
        the resulting position; the two differ by a single character and only the position's own
        dealId closes anything. Passing the confirmation id returns 404 and the position stays OPEN.
        That trap previously lived only in live_executor.close_order(), so any other caller of this
        method could silently leave a real position running. It is now handled here, for every caller.

        Resolution order:
          1. exact dealId match against the live open positions (an already-correct id);
          2. `epic`, when exactly one position is open on it;
          3. otherwise REFUSE.

        direction and size are accepted for interface parity with the old IGConnector; Capital.com's
        DELETE closes the whole position and does not need them.

        Fails CLOSED: if it cannot positively identify which position to close it returns False and
        fires no request, rather than firing a DELETE at a guessed id.
        """
        self._refresh_session()

        if not self._connected:
            log.error("close_position called but not connected")
            return False

        positions = self.get_open_positions() or []

        def _pos_of(p):
            return p.get("position", {}) or {}

        def _epic_of(p):
            return (p.get("market", {}) or {}).get("epic")

        target = None
        for p in positions:
            if deal_id and _pos_of(p).get("dealId") == deal_id:
                target = deal_id
                break

        if target is None and epic:
            matches = [p for p in positions if _epic_of(p) == epic]
            if len(matches) > 1:
                log.error("close_position: %d positions open on %s -- ambiguous, refusing to guess.",
                          len(matches), epic)
                return False
            if len(matches) == 1:
                target = _pos_of(matches[0]).get("dealId")
                if target != deal_id:
                    log.warning("close_position: id %s is not a live position (deal confirmation "
                                "reference?) -- resolved %s to its real dealId %s.",
                                deal_id, epic, target)

        if target is None:
            # Nothing to close is the desired end state, not a failure.
            if epic and not any(_epic_of(p) == epic for p in positions):
                log.info("close_position: no open %s position -- already closed.", epic)
                return True
            if not positions:
                log.info("close_position: account is flat -- nothing to close (given id %s).", deal_id)
                return True
            log.error("close_position: could not resolve a position for deal_id=%s epic=%r among %d "
                      "open position(s) -- refusing to fire a DELETE at an unverified id.",
                      deal_id, epic, len(positions))
            return False

        try:
            resp = requests.delete(
                f"{self._base_url}/positions/{target}",
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            deal_ref = resp.json().get("dealReference", "")
            log.info("Position closed | deal_id=%s | ref=%s", target, deal_ref)
            return True
        except Exception as exc:
            body = ""
            try:
                body = exc.response.text if getattr(exc, "response", None) is not None else ""
            except Exception:
                body = ""
            log.error("close_position failed for %s: %s %s", target, exc, body)
            # Re-verify: a broker stop/TP may have closed it underneath us.
            try:
                if not any(_pos_of(p).get("dealId") == target for p in (self.get_open_positions() or [])):
                    log.info("close_position: %s is gone on re-check -- treating as closed.", target)
                    return True
            except Exception:
                pass
            return False

    def get_open_positions(self):   # -> list | None (None = API/connection failure, NOT confirmed-empty)
        """Return list of currently open positions."""
        self._refresh_session()

        if not self._connected:
            return None
        try:
            resp = requests.get(
                f"{self._base_url}/positions",
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            positions = resp.json().get("positions", [])
            log.debug("Open positions: %d", len(positions))
            return positions
        except Exception as exc:
            log.error("get_open_positions failed: %s", exc)
            return None   # None = "could not determine" (distinct from [] confirmed-empty)

    def get_account_balance(self) -> Optional[float]:
        """Return current account balance."""
        self._refresh_session()

        if not self._connected:
            return None
        try:
            resp = requests.get(
                f"{self._base_url}/accounts",
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            accounts = resp.json().get("accounts", [])
            for acc in accounts:
                if acc.get("preferred"):
                    balance = acc.get("balance", {}).get("balance", 0.0)
                    log.info("Account balance: %.2f", balance)
                    return float(balance)
            if accounts:
                balance = accounts[0].get("balance", {}).get("balance", 0.0)
                log.info("Account balance: %.2f", balance)
                return float(balance)
            return None
        except Exception as exc:
            log.error("get_account_balance failed: %s", exc)
            return None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _confirm_deal(self, deal_reference: str) -> Optional[dict]:
        """Poll deal confirmation to get deal_id."""
        if not deal_reference:
            return None
        try:
            time.sleep(1)
            resp = requests.get(
                f"{self._base_url}/confirms/{deal_reference}",
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.warning("Deal confirmation failed for %s: %s", deal_reference, exc)
            return None


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log.info("Excalibur self-test -- checking Capital.com connection...")
    cap = CapitalComConnector()
    connected = cap.connect()
    if connected:
        log.info("Connection OK")
        bal = cap.get_account_balance()
        if bal is not None:
            log.info("Account balance: %.2f", bal)
        price = cap.get_price()
        if price:
            log.info("FTSE price: bid=%.1f ask=%.1f mid=%.1f", price["bid"], price["ask"], price["mid"])
        positions = cap.get_open_positions() or []
        log.info("Open positions: %d", len(positions))
    else:
        log.warning("Not connected -- check CAPITALCOM_* credentials in .env")
    log.info("Excalibur self-test complete.")
