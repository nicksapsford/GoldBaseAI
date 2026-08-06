"""
GoldTrader AI -- data_feed_gold.py  (Merlin)
Gold spot (XAU/USD) three-timeframe data feed with full indicator suite.
Primary: Capital.com API (epic GOLD). Fallback: Yahoo Finance (GC=F gold futures).
Timeframes: 1d (daily trend), 1h (confirmation), 5m (entry timing).

Gold trades ~23h/day Mon-Fri. No session-phase concept -- instead we track
LIQUIDITY PERIODS (Asian / London / New York / Overlap / Closing).
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("GoldTrader.Merlin")

GOLD_EPIC    = "GOLD"     # Capital.com epic for Gold spot (verified against demo API 2026-07-06)
GOLD_TICKER  = "GC=F"     # Yahoo Finance fallback (COMEX gold futures)
RATE_LIMIT_S = 1.0
YF_TIMEOUT_S = 15         # hard timeout for any single Yahoo Finance download

# ── Liquidity period constants (UTC) ──────────────────────────────────────────
ASIAN    = "ASIAN"        # 21:00-08:00 UTC -- lower liquidity, choppier
LONDON   = "LONDON"       # 08:00-13:00 UTC -- good liquidity, trends start
OVERLAP  = "OVERLAP"      # 13:00-17:00 UTC -- London+NY overlap, most active
NEW_YORK = "NEW_YORK"     # 17:00-20:30 UTC -- high volume
CLOSING  = "CLOSING"      # 20:30-21:00 UTC -- pre-close, no entries
CLOSED   = "CLOSED"       # 21:00-22:00 daily break + weekend

# Daily break: 21:00-22:00 UTC. Weekly: opens Sun 22:00 UTC, closes Fri 21:00 UTC.
DAILY_BREAK_START_MIN = 21 * 60   # 21:00 UTC
DAILY_BREAK_END_MIN   = 22 * 60   # 22:00 UTC


# ── Market hours / liquidity logic ────────────────────────────────────────────

def is_market_open(ts_utc: Optional[datetime] = None) -> bool:
    """
    True when the gold market is open.
    Open ~23h/day Mon-Fri. Closed during the 21:00-22:00 UTC daily break,
    all day Saturday, Sunday before 22:00 UTC, and Friday after 21:00 UTC.
    """
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc)
    wd = ts_utc.weekday()          # Mon=0 .. Sun=6
    hm = ts_utc.hour * 60 + ts_utc.minute

    if wd == 5:                    # Saturday -- closed all day
        return False
    if wd == 6:                    # Sunday -- opens 22:00 UTC
        return hm >= DAILY_BREAK_END_MIN
    if wd == 4:                    # Friday -- closes 21:00 UTC for the week
        return hm < DAILY_BREAK_START_MIN
    # Mon-Thu: open except the 21:00-22:00 daily break
    if DAILY_BREAK_START_MIN <= hm < DAILY_BREAK_END_MIN:
        return False
    return True


def get_liquidity_period(ts_utc: Optional[datetime] = None) -> str:
    """
    Return the current liquidity period label. CLOSED when the market is shut
    (weekend or the 21:00-22:00 daily break).
    """
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc)
    if not is_market_open(ts_utc):
        return CLOSED
    hm = ts_utc.hour * 60 + ts_utc.minute
    if hm >= 21 * 60 or hm < 8 * 60:      # 21:00-08:00 (break already filtered out)
        return ASIAN
    if hm < 13 * 60:                       # 08:00-13:00
        return LONDON
    if hm < 17 * 60:                       # 13:00-17:00
        return OVERLAP
    if hm < 20 * 60 + 30:                  # 17:00-20:30
        return NEW_YORK
    return CLOSING                         # 20:30-21:00


def liquidity_note(period: str) -> str:
    """Plain-English note about a liquidity period (for Arthur + dashboard)."""
    return {
        ASIAN:    "Lower volume, wider spreads, choppier -- require higher conviction.",
        LONDON:   "Good liquidity, trends often start here.",
        OVERLAP:  "London/NY overlap -- most active, best trading conditions of the day.",
        NEW_YORK: "Highest volume, strongest trends.",
        CLOSING:  "Pre-close volatility -- no new entries.",
        CLOSED:   "Market closed (daily break or weekend).",
    }.get(period, "")


def minutes_until_next_open(ts_utc: Optional[datetime] = None) -> int:
    """Minutes until the market next opens (0 if already open)."""
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc)
    if is_market_open(ts_utc):
        return 0
    # Search forward minute-by-minute (bounded, cheap) up to ~3 days.
    probe = ts_utc.replace(second=0, microsecond=0)
    for i in range(1, 3 * 24 * 60 + 1):
        probe = probe + timedelta(minutes=1)
        if is_market_open(probe):
            return i
    return 60


# ── Indicator calculations (identical suite to AlbionTrader) ──────────────────

def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _calc_macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast    = series.ewm(span=fast, adjust=False).mean()
    ema_slow    = series.ewm(span=slow, adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({
        "macd":      macd_line,
        "signal":    signal_line,
        "histogram": macd_line - signal_line,
    })


def _calc_ssl_cloud(df: pd.DataFrame, period: int = 10) -> pd.DataFrame:
    sma_high = df["high"].rolling(period).mean()
    sma_low  = df["low"].rolling(period).mean()
    hlv      = pd.Series(
        np.where(df["close"] > sma_high, 1,
        np.where(df["close"] < sma_low, -1, np.nan)),
        index=df.index,
    ).ffill()
    ssl_up   = np.where(hlv < 0, sma_low,  sma_high)
    ssl_down = np.where(hlv < 0, sma_high, sma_low)
    return pd.DataFrame({
        "ssl_up":   ssl_up,
        "ssl_down": ssl_down,
        "ssl_bull": ssl_up > ssl_down,
    }, index=df.index)


def _calc_tmo(df: pd.DataFrame, length: int = 14, calc_length: int = 5) -> pd.DataFrame:
    mom    = np.sign(df["close"] - df["open"]).rolling(length).sum()
    main   = mom.ewm(span=calc_length, adjust=False).mean()
    smooth = main.ewm(span=calc_length, adjust=False).mean()
    return pd.DataFrame({"tmo_main": main, "tmo_smooth": smooth}, index=df.index)


def _calc_chande(df: pd.DataFrame, period: int = 20) -> pd.Series:
    diff   = df["close"].diff()
    up_sum = diff.clip(lower=0).rolling(period).sum()
    dn_sum = (-diff.clip(upper=0)).rolling(period).sum()
    denom  = (up_sum + dn_sum).replace(0, np.nan)
    return (100 * (up_sum - dn_sum) / denom).rename("chande_mo")


def _calc_money_flow(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].replace(0, np.nan)
    mfv = tp * vol * np.sign(df["close"] - df["open"])
    return (mfv.rolling(period).sum() / vol.rolling(period).sum()).rename("money_flow")


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Apply all 6 indicators to an OHLCV DataFrame. Returns enriched copy."""
    if df.empty:
        return df
    df = df.copy()
    df["rsi"]            = _calc_rsi(df["close"])
    macd_df              = _calc_macd(df["close"])
    df["macd"]           = macd_df["macd"]
    df["macd_signal"]    = macd_df["signal"]
    df["macd_histogram"] = macd_df["histogram"]
    ssl_df               = _calc_ssl_cloud(df)
    df["ssl_up"]         = ssl_df["ssl_up"]
    df["ssl_down"]       = ssl_df["ssl_down"]
    df["ssl_bull"]       = ssl_df["ssl_bull"]
    tmo_df               = _calc_tmo(df)
    df["tmo_main"]       = tmo_df["tmo_main"]
    df["tmo_smooth"]     = tmo_df["tmo_smooth"]
    df["chande_mo"]      = _calc_chande(df)
    df["money_flow"]     = _calc_money_flow(df)
    return df


def get_composite_signal(row: pd.Series) -> str:
    """Return LONG, SHORT, or NEUTRAL for a single bar."""
    signals = []
    if pd.notna(row.get("ssl_bull")):
        signals.append(1 if row["ssl_bull"] else -1)
    rsi = row.get("rsi")
    if pd.notna(rsi):
        signals.append(1 if rsi > 55 else (-1 if rsi < 45 else 0))
    hist = row.get("macd_histogram")
    if pd.notna(hist):
        signals.append(1 if hist > 0 else -1)
    tmo_main, tmo_smooth = row.get("tmo_main"), row.get("tmo_smooth")
    if pd.notna(tmo_main) and pd.notna(tmo_smooth):
        signals.append(1 if tmo_main > tmo_smooth else -1)
    cmo = row.get("chande_mo")
    if pd.notna(cmo):
        signals.append(1 if cmo > 0 else -1)
    mf = row.get("money_flow")
    if pd.notna(mf):
        signals.append(1 if mf > 0 else -1)
    if not signals:
        return "NEUTRAL"
    score = sum(signals) / len(signals)
    return "LONG" if score >= 0.5 else ("SHORT" if score <= -0.5 else "NEUTRAL")


# ── Yahoo Finance fallback (hard-timeout wrapped) ─────────────────────────────

def _yf_download_timed(ticker: str, period: str, interval: str,
                       timeout: float = YF_TIMEOUT_S) -> pd.DataFrame:
    """
    Run yf.download() in a worker thread and enforce a hard timeout.
    yfinance has no timeout parameter and can hang indefinitely on a stalled
    connection; a bounded join() lets us raise instead of freezing the loop.
    """
    import yfinance as yf
    result: list = [None]
    error:  list = [None]

    def fetch() -> None:
        try:
            result[0] = yf.download(ticker, period=period, interval=interval,
                                    auto_adjust=True, progress=False)
        except Exception as exc:            # noqa: BLE001 -- surfaced below
            error[0] = exc

    t = threading.Thread(target=fetch, name=f"yf-{interval}", daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise TimeoutError(
            f"Yahoo Finance timed out after {timeout}s ({ticker} {period} {interval})"
        )
    if error[0] is not None:
        raise error[0]
    return result[0]


def _fetch_yf(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """Fetch OHLCV from Yahoo Finance and normalise column names."""
    try:
        raw = _yf_download_timed(ticker, period, interval)
        if raw is None or raw.empty:
            return pd.DataFrame()
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [c[0].lower() for c in raw.columns]
        else:
            raw.columns = [c.lower() for c in raw.columns]
        raw = raw.rename(columns={"adj close": "close"})
        if raw.index.tz is None:
            raw.index = raw.index.tz_localize("UTC")
        else:
            raw.index = raw.index.tz_convert("UTC")
        for col in ["open", "high", "low", "close"]:
            if col in raw.columns:
                raw[col] = pd.to_numeric(raw[col], errors="coerce")
        if "volume" not in raw.columns:
            raw["volume"] = 0.0
        raw["volume"] = raw["volume"].fillna(0.0)
        return raw.dropna(subset=["close"])
    except Exception as exc:
        log.warning("Yahoo Finance fetch failed (%s %s %s): %s", ticker, period, interval, exc)
        return pd.DataFrame()


# ── Main GoldDataFeed class ───────────────────────────────────────────────────

class GoldDataFeed:
    """
    Merlin -- Gold spot three-timeframe data feed.

    Usage:
        feed = GoldDataFeed(ig_connector)   # CapitalComConnector or None for Yahoo fallback
        feed.initialise()                   # full history once
        bar_1d = feed.latest_bar("1d")
        feed.refresh()                      # small recent window, merged into cache
    """

    def __init__(self, ig_connector=None) -> None:
        self._ig = ig_connector
        self._use_ig = ig_connector is not None and getattr(ig_connector, "connected", False)
        self._frames: dict[str, pd.DataFrame] = {}
        log.info(
            "Merlin initialising | source=%s",
            "Capital.com (GOLD)" if self._use_ig else "Yahoo Finance (GC=F fallback)",
        )

    def _fetch_ig(self, resolution: str, num_points: int) -> pd.DataFrame:
        if self._ig is None:
            return pd.DataFrame()
        df = self._ig.get_historical_prices(GOLD_EPIC, resolution, num_points)
        if df is None or df.empty:
            return pd.DataFrame()
        return df

    # Full history is fetched once at startup; every refresh() thereafter only
    # pulls a small recent window (recent=True) and merges it into the cached
    # frame -- so we never re-download hundreds/thousands of bars each tick.
    def _fetch_1d(self, recent: bool = False) -> pd.DataFrame:
        if self._use_ig:
            df = self._fetch_ig("DAY", 10 if recent else 200)
            if not df.empty:
                return df
        log.info("Falling back to Yahoo Finance for 1d data")
        return _fetch_yf(GOLD_TICKER, "5d" if recent else "2y", "1d")

    def _fetch_1h(self, recent: bool = False) -> pd.DataFrame:
        if self._use_ig:
            df = self._fetch_ig("HOUR", 10 if recent else 200)
            if not df.empty:
                return df
        log.info("Falling back to Yahoo Finance for 1h data")
        return _fetch_yf(GOLD_TICKER, "5d" if recent else "90d", "1h")

    def _fetch_5m(self, recent: bool = False) -> pd.DataFrame:
        if self._use_ig:
            df = self._fetch_ig("MINUTE_5", 20 if recent else 200)
            if not df.empty:
                return df
        log.info("Falling back to Yahoo Finance for 5m data")
        period = "1d" if recent else "60d"
        df = _fetch_yf(GOLD_TICKER, period, "5m")
        if not df.empty:
            return df
        log.info("5m fallback: trying 15m")
        return _fetch_yf(GOLD_TICKER, period, "15m")

    def initialise(self) -> None:
        """Full historical load at startup. Builds all three timeframes."""
        log.info("=== Merlin initialising data feed (Gold) ===")
        for tf, fetch_fn in [("1d", self._fetch_1d), ("1h", self._fetch_1h), ("5m", self._fetch_5m)]:
            log.info("  Fetching %s...", tf)
            df = fetch_fn()
            if df.empty:
                log.warning("  [%s] No data returned", tf)
                self._frames[tf] = pd.DataFrame()
                continue
            df = add_indicators(df)
            self._frames[tf] = df
            self._log_tf(tf, df, "candles")
            time.sleep(RATE_LIMIT_S)
        log.info("Merlin ready -- all timeframes loaded")

    def refresh(self) -> None:
        """Incremental update -- fetch a small recent window and merge into cache."""
        log.info("=== Merlin refreshing ===")
        for tf, fetch_fn in [("1d", self._fetch_1d), ("1h", self._fetch_1h), ("5m", self._fetch_5m)]:
            new_df = fetch_fn(recent=True)   # small recent window only
            if new_df.empty:
                log.warning("  [%s] No new data", tf)
                continue
            if tf in self._frames and not self._frames[tf].empty:
                combined = pd.concat([self._frames[tf], new_df])
                combined = combined[~combined.index.duplicated(keep="last")]
                combined.sort_index(inplace=True)
                combined = add_indicators(combined)
                self._frames[tf] = combined
            else:
                self._frames[tf] = add_indicators(new_df)
            self._log_tf(tf, self._frames[tf], "bars")
            time.sleep(RATE_LIMIT_S)

    def _log_tf(self, tf: str, df: pd.DataFrame, unit: str) -> None:
        bar   = df.iloc[-1]
        ssl   = "BULL" if bar.get("ssl_bull") else "BEAR"
        rsi   = bar.get("rsi", 0)
        close = bar.get("close", 0)
        log.info("  [%s] %d %s | close=%.2f | rsi=%.1f | ssl=%s",
                 tf, len(df), unit, close, rsi if pd.notna(rsi) else 0, ssl)

    def get(self, timeframe: str = "5m") -> pd.DataFrame:
        if timeframe not in self._frames:
            raise KeyError(f"Timeframe '{timeframe}' not loaded. Call initialise() first.")
        return self._frames[timeframe].copy()

    def get_historical_price(self, market, timestamp_utc):
        """Return the 5m candle close at/just before timestamp_utc from the cached
        frame -- used to resolve stale phantom PENDING rows on restart. float|None."""
        try:
            df = self._frames.get("5m")
            if df is None or df.empty:
                return None
            ts = pd.Timestamp(timestamp_utc)
            ts = ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
            sub = df[df.index <= ts]
            if sub.empty:
                return None
            return float(sub["close"].iloc[-1])
        except Exception as exc:
            log.warning("Merlin get_historical_price error: %s", exc)
            return None

    def latest_bar(self, timeframe: str = "5m") -> pd.Series:
        df = self.get(timeframe)
        if df.empty:
            raise ValueError(f"No data available for timeframe {timeframe}")
        return df.iloc[-1]

    def composite_signal(self, timeframe: str = "5m") -> str:
        return get_composite_signal(self.latest_bar(timeframe))

    def print_status(self) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        period = get_liquidity_period()
        log.info("-" * 60)
        log.info("Merlin -- Gold Data Feed Status | %s", now)
        log.info("  Liquidity period: %s | Market open: %s", period, is_market_open())
        for tf in ("1d", "1h", "5m"):
            if tf not in self._frames or self._frames[tf].empty:
                log.info("  [%s] No data", tf)
                continue
            bar = self.latest_bar(tf)
            sig = get_composite_signal(bar)
            rsi = bar.get("rsi", 0)
            log.info("  [%s] close=%.2f | rsi=%.1f | ssl=%s | tmo=%.3f | -> %s",
                     tf, bar["close"], rsi if pd.notna(rsi) else 0,
                     "BULL" if bar.get("ssl_bull") else "BEAR",
                     bar.get("tmo_main", 0) if pd.notna(bar.get("tmo_main")) else 0, sig)
        log.info("-" * 60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log.info("Merlin self-test (Gold, Yahoo Finance fallback)...")
    feed = GoldDataFeed(ig_connector=None)
    feed.initialise()
    feed.print_status()
    log.info("Liquidity period: %s | Market open: %s", get_liquidity_period(), is_market_open())
    log.info("Merlin self-test complete.")
