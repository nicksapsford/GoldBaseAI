"""
compass/backtest_engine.py -- Merlin's Compass Session 2: Gold signal-replay backtester.
================================================================================
Replays 2 years of cached Gold 5-minute candles through the LIVE GoldBase/Lancelot rules and reports the
BASELINE performance (win rate, profit factor, MFE distribution) BEFORE any Compass gate is added.

FIDELITY BY REUSE -- this does NOT re-implement the strategy. It imports and calls the real modules:
  * data_feed_gold.add_indicators  -> the exact SSL / RSI / MACD / TMO indicators
  * pre_checks_gold.check_*        -> the exact Lancelot entry gate (SSL agree, 1h RSI, 5m TMO, choppy, candle)
  * strategy_gold.GoldTrade        -> the exact 40pt stop / 25pt TP / two-speed trail / 0.3pt spread / force-close

MODELLING CHOICES (documented so the numbers are honest):
  1. NO LOOKAHEAD. At each 5-min bar the 1h and daily SSL come from the last bar that had already CLOSED
     (1h: start+1h <= now; daily: start+1d <= now, i.e. yesterday's daily intraday). Live uses the forming
     daily bar; this backtest is deliberately conservative. The Compass comparison (Session 3) uses the same
     alignment, so the RELATIVE effect is unaffected.
  2. INTRABAR = PESSIMISTIC. Within a 5-min bar the adverse extreme is assumed to hit first (stop before TP),
     so winners are, if anything, understated.
  3. STATEFUL SAFETY OVERLAYS (kill switch, daily-loss limit, consecutive-loss limit) are NOT modelled -- they
     only ever REDUCE trading, never add edge. The 30-min post-loss COOLDOWN is modelled (it shapes the
     trade sequence). Sizing is the fixed paper stake; all edge metrics are in POINTS (size-independent).
  4. Entry fills at the signal bar's close; spread (0.3pt) is charged at exit by the real calculate_pnl.
"""
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

try:
    from . import polygon_fetcher as pf
except ImportError:
    import polygon_fetcher as pf
import data_feed_gold as feed
import pre_checks_gold as pc
import strategy_gold as strat

logging.disable(logging.CRITICAL)     # silence the per-check log.info spam during the replay

NO_ENTRY_AFTER_MIN = 20 * 60          # 20:00 UTC (matches pre_checks.NO_ENTRY_AFTER_MIN)
COOLDOWN_SECS = 30 * 60               # 30-min post-loss cooldown
GAP_SECS = 90 * 60                    # >90-min bar gap => a break/weekend; force out an open trade
BT_GBPUSD = 1.27                      # fixed (GBP-settled; only affects the retired usd column)


def _enriched(tf: str) -> pd.DataFrame:
    df = pd.DataFrame(pf.load(tf))
    df["dt"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("dt").reset_index(drop=True)
    df = feed.add_indicators(df)
    # resolution-independent UTC epoch seconds (the dt column can be us- or ns-resolution)
    df["epoch"] = ((df["dt"] - pd.Timestamp("1970-01-01T00:00:00Z")) // pd.Timedelta(seconds=1)).astype("int64")
    return df


def run_baseline(verbose: bool = True) -> list:
    five = _enriched("5min"); hour = _enriched("1hour"); daily = _enriched("daily")

    ft = five["epoch"].to_numpy()
    fo, fh, fl, fc = (five[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close"))
    f5_ssl = five["ssl_bull"].to_numpy()
    f5_rsi = five["rsi"].to_numpy(dtype=float)
    f5_tmo = five["tmo_main"].to_numpy(dtype=float)

    ht = hour["epoch"].to_numpy(); h_ssl = hour["ssl_bull"].to_numpy(); h_rsi = hour["rsi"].to_numpy(dtype=float)
    dt_ = daily["epoch"].to_numpy(); d_ssl = daily["ssl_bull"].to_numpy()

    # no-lookahead alignment: last CLOSED 1h / daily bar for each 5-min bar
    hidx_of = np.searchsorted(ht, ft - 3600, side="right") - 1
    didx_of = np.searchsorted(dt_, ft - 86400, side="right") - 1

    trades = []
    in_trade = False
    trade = None
    direction = None
    cooldown_until = 0
    n = len(five)

    for i in range(n):
        hidx, didx = hidx_of[i], didx_of[i]

        # ── manage an open trade ──
        if in_trade:
            # a data gap (weekend / daily break) while still open -> force out at the previous bar's close
            if i > 0 and (ft[i] - ft[i - 1]) > GAP_SECS:
                trade.close(fc[i - 1], "FORCE_CLOSE", BT_GBPUSD)
                _record(trades, trade, direction); in_trade = False
                if trade.pnl_gbp < 0:
                    cooldown_until = ft[i - 1] + COOLDOWN_SECS
                # fall through: this bar may open a new trade below
            else:
                now = datetime.fromtimestamp(int(ft[i]), timezone.utc)
                hi, lo, op = fh[i], fl[i], fc[i]
                if strat.should_force_close(now):
                    trade.close(fo[i], "FORCE_CLOSE", BT_GBPUSD)
                    _record(trades, trade, direction); in_trade = False
                    if trade.pnl_gbp < 0:
                        cooldown_until = ft[i] + COOLDOWN_SECS
                    continue
                trade.update_excursions(hi); trade.update_excursions(lo)   # MFE/MAE (bar extremes)
                exit_reason, exit_price = None, None
                if direction == "LONG":
                    if lo <= trade.stop_loss:              exit_reason, exit_price = "STOP_LOSS", trade.stop_loss
                    elif hi >= trade.take_profit:          exit_reason, exit_price = "TAKE_PROFIT", trade.take_profit
                    else:                                  trade.update_trailing_stop(hi)
                else:
                    if hi >= trade.stop_loss:              exit_reason, exit_price = "STOP_LOSS", trade.stop_loss
                    elif lo <= trade.take_profit:          exit_reason, exit_price = "TAKE_PROFIT", trade.take_profit
                    else:                                  trade.update_trailing_stop(lo)
                if exit_reason:
                    trade.close(exit_price, exit_reason, BT_GBPUSD)
                    _record(trades, trade, direction); in_trade = False
                    if trade.pnl_gbp < 0:
                        cooldown_until = ft[i] + COOLDOWN_SECS
                continue

        # ── look for an entry (flat only) ──
        if hidx < 12 or didx < 12:
            continue
        # warmup: indicators must be valid
        if np.isnan(f5_rsi[i]) or np.isnan(f5_tmo[i]) or np.isnan(h_rsi[hidx]):
            continue
        if ft[i] < cooldown_until:
            continue
        now = datetime.fromtimestamp(int(ft[i]), timezone.utc)
        if not feed.is_market_open(now):
            continue
        hm = now.hour * 60 + now.minute
        if NO_ENTRY_AFTER_MIN <= hm < 21 * 60:          # near-close: no new entries after 20:00 UTC
            continue

        d, h, m = bool(d_ssl[didx]), bool(h_ssl[hidx]), bool(f5_ssl[i])
        if not (d == h == m):                            # 3-timeframe SSL agreement (the direction signal)
            continue
        direction = "LONG" if d else "SHORT"

        if direction == "SHORT":                         # daily filter: SHORT needs 3 sustained BEAR daily bars
            recent = d_ssl[max(0, didx - 2): didx + 1]
            if d_ssl[didx] or len(recent) < 3 or bool(np.any(recent)):
                continue

        bar1h = {"ssl_bull": h, "rsi": h_rsi[hidx]}
        bar5m = {"ssl_bull": m, "rsi": f5_rsi[i], "tmo_main": f5_tmo[i], "open": fo[i], "close": fc[i]}
        is_asian = hm >= 21 * 60 or hm < 8 * 60
        # the real Lancelot quality gate (reused verbatim)
        if not pc.check_ssl_agreement(bar1h, bar5m, direction)["passed"]:      continue
        if not pc.check_1h_rsi_confirms(bar1h, direction, is_asian)["passed"]: continue
        if not pc.check_5m_tmo_momentum(bar1h, bar5m)["passed"]:              continue
        if not pc.check_choppy_market(bar1h, bar5m)["passed"]:                continue
        if not pc.check_candle_confirmed(bar1h, bar5m)["passed"]:             continue

        trade = strat.GoldTrade(direction=direction, entry_price=fc[i], stop_pts=strat.TRAILING_STOP_POINTS,
                                gbpusd_entry=BT_GBPUSD, entry_time=now, balance=0.0)
        in_trade = True

    if verbose:
        print("replay complete: %d 5-min bars -> %d trades" % (n, len(trades)))
    return trades


def _record(trades, trade, direction):
    trades.append({
        "direction": direction,
        "entry": round(trade.entry_price, 2),
        "exit": round(trade.exit_price, 2),
        "reason": trade.exit_reason,
        "points": trade.points_gained,       # net of 0.3pt spread
        "pnl_gbp": trade.pnl_gbp,             # points * fixed stake (size cancels in PF / win-rate)
        "mfe": trade.mfe_pts,
        "mae": trade.mae_pts,
        "entry_time": trade.entry_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


if __name__ == "__main__":
    from compass.backtest_report import report
    report(run_baseline())
