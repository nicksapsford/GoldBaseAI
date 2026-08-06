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


class CapitalComConnector:
    """
    Excalibur -- Capital.com API connector.

    Usage:
        cap = CapitalComConnector()
        if cap.connect():
            price = cap.get_price("FTSE")
    """

    def __init__(self) -> None:
        self._connected           = False
        self._price_cache: dict   = {}   # epic -> (price_dict, timestamp)

        self._cst                 = None
        self._security_token      = None
        self._session_created_at  = None   # time.monotonic() at last (re)auth

        self._email    = os.getenv("CAPITALCOM_EMAIL",    "")
        self._password = os.getenv("CAPITALCOM_PASSWORD", "")
        self._api_key  = os.getenv("CAPITALCOM_API_KEY",  "")
        self._acc_type = os.getenv("CAPITALCOM_ACC_TYPE", "DEMO")

        if not all([self._email, self._password, self._api_key]):
            log.warning(
                "Capital.com credentials not fully configured. "
                "Set CAPITALCOM_EMAIL, CAPITALCOM_PASSWORD, CAPITALCOM_API_KEY in .env"
            )

        self._base_url = (
            LIVE_BASE_URL if self._acc_type.strip().upper() == "LIVE" else DEMO_BASE_URL
        )

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

                log.info(
                    "Excalibur connected to Capital.com (%s)",
                    self._acc_type.strip().upper() or "DEMO",
                )
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
            bid  = float(snap.get("bid", 0))
            ask  = float(snap.get("offer", 0))
            mid  = round((bid + ask) / 2, 1)
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
                rows = []
                for p in prices:
                    snap_time = p.get("snapshotTime", "")
                    o_mid = (float(p["openPrice"]["bid"]) + float(p["openPrice"]["ask"])) / 2
                    h_mid = (float(p["highPrice"]["bid"]) + float(p["highPrice"]["ask"])) / 2
                    l_mid = (float(p["lowPrice"]["bid"]) + float(p["lowPrice"]["ask"])) / 2
                    c_mid = (float(p["closePrice"]["bid"]) + float(p["closePrice"]["ask"])) / 2
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

        try:
            resp = requests.post(
                f"{self._base_url}/positions",
                headers=self._headers(),
                json={
                    "direction":       direction,
                    "epic":            epic,
                    "size":            size,
                    "guaranteedStop":  False,
                    "stopDistance":    stop_distance,
                    "stopLevel":       None,
                    "profitDistance":  None,
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
            deal_id = confirm.get("dealId", "") if confirm else ""
            return {"deal_reference": deal_ref, "deal_id": deal_id}
        except Exception as exc:
            log.error("open_position failed: %s", exc)
            return None

    def close_position(
        self,
        deal_id: str,
        direction: str,
        size: float,
    ) -> bool:
        """
        Close an open position by deal_id.
        direction and size are accepted for interface parity with the old
        IGConnector -- Capital.com's DELETE /positions/{dealId} closes the
        whole position and does not require them.
        Returns True on success.
        """
        self._refresh_session()

        if not self._connected:
            log.error("close_position called but not connected")
            return False

        try:
            resp = requests.delete(
                f"{self._base_url}/positions/{deal_id}",
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            deal_ref = resp.json().get("dealReference", "")
            log.info("Position closed | deal_id=%s | ref=%s", deal_id, deal_ref)
            return True
        except Exception as exc:
            log.error("close_position failed: %s", exc)
            return False

    def get_open_positions(self) -> list:
        """Return list of currently open positions."""
        self._refresh_session()

        if not self._connected:
            return []
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
            return []

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
        positions = cap.get_open_positions()
        log.info("Open positions: %d", len(positions))
    else:
        log.warning("Not connected -- check CAPITALCOM_* credentials in .env")
    log.info("Excalibur self-test complete.")
