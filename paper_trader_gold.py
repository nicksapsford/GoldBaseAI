"""
GoldTrader AI -- paper_trader_gold.py  (Stanley)
Records every Gold spread bet and tracks running P&L in GBP.
Persists state between sessions via logs/gold_trades.csv.
"""

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from strategy_gold import GoldTrade, TRAILING_STOP_POINTS, DEFAULT_GBPUSD

log = logging.getLogger("GoldTrader.Stanley")

STARTING_CAPITAL_GBP = 1000.0
LOG_DIR      = Path(__file__).parent / "logs"
TRADES_LOG   = LOG_DIR / "gold_trades.csv"
SUMMARY_LOG  = LOG_DIR / "gold_summary.txt"
STATE_FILE   = LOG_DIR / "stanley_gold_state.json"

CSV_HEADERS = [
    "date", "time", "direction",
    "entry_price_usd", "exit_price_usd",
    "stake_per_point", "points_gained", "pnl_usd", "pnl_gbp", "gbpusd_rate",
    "exit_reason", "capital_after_gbp",
    "entry_time", "exit_time", "liquidity_period",
]


class PaperTraderGold:
    """Stanley -- paper trading accountant for Gold spread bets."""

    def __init__(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not TRADES_LOG.exists():
            self._init_csv()
            log.info("Created new trades log: %s", TRADES_LOG)
        else:
            log.info("Using existing trades log: %s", TRADES_LOG)

        self.capital_gbp   = STARTING_CAPITAL_GBP
        self.current_trade: Optional[GoldTrade] = None
        self.trade_history: list[GoldTrade]     = []
        self._gbpusd = DEFAULT_GBPUSD

        previous_capital = self._load_last_capital()
        if previous_capital:
            self.capital_gbp = previous_capital
            log.info("Resumed | capital=GBP %.2f", self.capital_gbp)
        else:
            log.info("Fresh start | capital=GBP %.2f", STARTING_CAPITAL_GBP)

        self._restore_state()
        log.info("Stanley ready -- Gold paper trader")

    # ── CSV management ────────────────────────────────────────────────────────

    def _init_csv(self) -> None:
        with open(TRADES_LOG, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()

    def _load_last_capital(self) -> Optional[float]:
        if not TRADES_LOG.exists():
            return None
        try:
            df = pd.read_csv(TRADES_LOG)
            if df.empty:
                return None
            return float(df["capital_after_gbp"].iloc[-1])
        except Exception:
            return None

    def _save_state(self) -> None:
        if self.current_trade is None:
            self._clear_state()
            return
        t = self.current_trade
        state = {
            "direction":        t.direction,
            "entry_price":      t.entry_price,
            "stop_pts":         t.stop_pts,
            "size_oz":          t.size_oz,
            "gbpusd_entry":     t.gbpusd_entry,
            "entry_time":       t.entry_time.isoformat() if t.entry_time else None,
            "liquidity_period": t.liquidity_period,
            "trail_best":       t.trail_best,
            "stop_loss":        t.stop_loss,
            "take_profit":      t.take_profit,
            "stake":            t.stake,
        }
        try:
            STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("Could not save state: %s", exc)

    def _clear_state(self) -> None:
        try:
            if STATE_FILE.exists():
                STATE_FILE.unlink()
        except Exception:
            pass

    def _restore_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            from strategy_gold import should_force_close
            if should_force_close():
                log.info("State file found but force-close window active -- discarding stale state")
                self._clear_state()
                return
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            trade = GoldTrade(
                direction        = data["direction"],
                entry_price      = data["entry_price"],
                stop_pts         = data["stop_pts"],
                size_oz          = data.get("size_oz", 0.0),
                gbpusd_entry     = data.get("gbpusd_entry", DEFAULT_GBPUSD),
                entry_time       = datetime.fromisoformat(data["entry_time"]) if data.get("entry_time") else None,
                liquidity_period = data.get("liquidity_period", ""),
            )
            trade.trail_best  = data["trail_best"]
            trade.stop_loss   = data["stop_loss"]
            trade.take_profit = data["take_profit"]
            trade.stake       = data["stake"]
            self.current_trade = trade
            log.info(
                "STATE RESTORED: %s entry=$%.2f stop=$%.2f size=%.2foz",
                trade.direction, trade.entry_price, trade.stop_loss, trade.size_oz,
            )
        except Exception as exc:
            log.warning("Could not restore state (%s) -- starting fresh", exc)
            self._clear_state()

    def _log_trade(self, trade: GoldTrade) -> None:
        if trade.exit_price is None:
            return
        exit_t  = trade.exit_time or datetime.now(timezone.utc)
        entry_t = trade.entry_time or exit_t
        row = {
            "date":              exit_t.strftime("%Y-%m-%d"),
            "time":              exit_t.strftime("%H:%M:%S"),
            "direction":         trade.direction,
            "entry_price_usd":   f"{trade.entry_price:.2f}",
            "exit_price_usd":    f"{trade.exit_price:.2f}",
            "stake_per_point":   f"{trade.stake:.4f}",
            "points_gained":     f"{trade.points_gained:+.2f}",
            "pnl_usd":           f"{trade.pnl_usd:+.2f}",
            "pnl_gbp":           f"{trade.pnl_gbp:+.2f}",
            "gbpusd_rate":       f"{trade.gbpusd_exit:.5f}",
            "exit_reason":       trade.exit_reason,
            "capital_after_gbp": f"{self.capital_gbp:.2f}",
            "entry_time":        entry_t.strftime("%Y-%m-%d %H:%M:%S"),
            "exit_time":         exit_t.strftime("%Y-%m-%d %H:%M:%S"),
            "liquidity_period":  trade.liquidity_period,
        }
        with open(TRADES_LOG, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_HEADERS).writerow(row)
        log.info("Trade logged: %s", TRADES_LOG)

    def _save_summary(self) -> None:
        total    = len(self.trade_history)
        winners  = sum(1 for t in self.trade_history if (t.pnl_gbp or 0) >= 0)
        win_rate = (winners / total * 100) if total > 0 else 0.0
        total_pnl = sum(t.pnl_gbp for t in self.trade_history if t.pnl_gbp is not None)
        lines = [
            "=" * 50,
            "GoldTrader AI -- Stanley Paper Trader Summary",
            "Generated: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "=" * 50,
            f"Starting capital:  GBP {STARTING_CAPITAL_GBP:.2f}",
            f"Current capital:   GBP {self.capital_gbp:.2f}",
            f"Total P&L:         GBP {total_pnl:+.2f}",
            f"Total return:      {(self.capital_gbp / STARTING_CAPITAL_GBP - 1) * 100:+.2f}%",
            "",
            f"Total trades:      {total}",
            f"Winning trades:    {winners}",
            f"Win rate:          {win_rate:.1f}%",
            "",
        ]
        if self.trade_history:
            lines.append("Recent trades (last 10):")
            lines.append("-" * 50)
            for t in self.trade_history[-10:]:
                result = "WIN " if (t.pnl_gbp or 0) >= 0 else "LOSS"
                lines.append(
                    f"  [{result} {t.direction}] {t.liquidity_period} | "
                    f"entry=${t.entry_price:,.2f} exit=${t.exit_price:,.2f} "
                    f"pts={t.points_gained:+.1f} P&L=GBP {t.pnl_gbp:+.2f} "
                    f"reason={t.exit_reason}"
                )
        lines.append("=" * 50)
        with open(SUMMARY_LOG, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    # ── Trade management ──────────────────────────────────────────────────────

    @property
    def in_trade(self) -> bool:
        return self.current_trade is not None

    @property
    def total_trades(self) -> int:
        return len(self.trade_history)

    @property
    def winning_trades(self) -> int:
        return sum(1 for t in self.trade_history if (t.pnl_gbp or 0) >= 0)

    @property
    def win_rate(self) -> float:
        if not self.trade_history:
            return 0.0
        return self.winning_trades / len(self.trade_history) * 100

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl_gbp for t in self.trade_history if t.pnl_gbp is not None)

    def open_trade(self, direction: str, price: float, gbpusd: float,
                   liquidity_period: str = "") -> GoldTrade:
        """Open a new paper trade and log it."""
        from strategy_gold import open_trade
        self._gbpusd = gbpusd
        self.current_trade = open_trade(direction, price, gbpusd, liquidity_period)
        self._save_state()
        log.info(
            "[OPEN] %s | entry=$%.2f | size=%.2foz | stake=£%.4f/pt | stop=$%.2f | target=$%.2f",
            direction, price, self.current_trade.size_oz, self.current_trade.stake,
            self.current_trade.stop_loss, self.current_trade.take_profit,
        )
        return self.current_trade

    def close_trade(self, price: float, reason: str, gbpusd: float = None) -> Optional[GoldTrade]:
        """Close the current paper trade, update capital, save CSV."""
        if self.current_trade is None:
            return None
        rate = gbpusd if gbpusd is not None else self._gbpusd
        # stop-fill fidelity (Job 10, 24 Jul 2026, Nick-confirmed 23 Jul): on a
        # STOP_LOSS exit ONLY, fill at the stop level rather than the observed price,
        # which may have gapped through the stop between 30s monitor checks. Clamp the
        # fill BEFORE P&L is computed so it feeds BOTH the USD and GBP P&L (strategy_gold
        # .close sets exit_price = this price and derives pnl_usd/pnl_gbp from it via
        # calculate_pnl). TAKE_PROFIT / ARTHUR_EXIT / FORCE_CLOSE keep the observed price.
        if reason == "STOP_LOSS":
            if self.current_trade.direction == "LONG":
                price = max(self.current_trade.stop_loss, price)
            else:
                price = min(self.current_trade.stop_loss, price)
        from strategy_gold import close_trade
        trade = close_trade(self.current_trade, price, reason, rate)
        self.capital_gbp = round(self.capital_gbp + trade.pnl_gbp, 2)
        self.trade_history.append(trade)
        self._log_trade(trade)
        self._save_summary()
        self._clear_state()
        result = "PROFIT" if trade.pnl_gbp >= 0 else "LOSS"
        log.info(
            "[%s] Trade complete | %s | pts=%+.1f | P&L=GBP %+.2f | capital=GBP %.2f",
            result, trade.direction, trade.points_gained, trade.pnl_gbp, self.capital_gbp,
        )
        self.current_trade = None
        return trade

    def monitor_trade(self, price: float, gbpusd: float = None) -> Optional[str]:
        """Update trailing stop and check for exit. Returns exit reason if closed."""
        if self.current_trade is None:
            return None
        if gbpusd is not None:
            self._gbpusd = gbpusd
        moved = self.current_trade.update_trailing_stop(price)
        rung = self.current_trade.apply_profit_ladder(price)   # Profit ladder (Variant 2)
        if rung:
            self._log_ladder_step(rung)
            moved = True
        if moved:
            self._save_state()
        reason = self.current_trade.check_exit(price)
        if reason:
            self.close_trade(price, reason, self._gbpusd)
            return reason
        return None

    def _log_ladder_step(self, rung):
        """Append a profit-ladder rung trigger to logs/profit_ladder.csv (Variant 2)."""
        import csv, os
        from datetime import datetime, timezone
        t = self.current_trade
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "profit_ladder.csv")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["timestamp_utc", "system", "direction",
                    "entry_price", "trigger_float_gbp", "floor_gbp", "step_number",
                    "stop_before", "stop_after"])
                if new:
                    w.writeheader()
                w.writerow({"timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "system": "GoldTrader", "direction": t.direction, "entry_price": t.entry_price,
                    "trigger_float_gbp": rung["trigger_float_gbp"], "floor_gbp": rung["floor_gbp"],
                    "step_number": rung["step"], "stop_before": rung["stop_before"],
                    "stop_after": rung["stop_after"]})
        except Exception as exc:
            log.warning("Could not log ladder step: %s", exc)

    def print_status(self) -> None:
        log.info("=" * 60)
        log.info("GoldTrader AI -- Stanley Paper Trader Status")
        log.info("-" * 60)
        log.info("  Starting capital:  GBP %.2f", STARTING_CAPITAL_GBP)
        log.info("  Current capital:   GBP %.2f", self.capital_gbp)
        log.info("  Total P&L:         GBP %+.2f", self.total_pnl)
        log.info("  Return:            %+.2f%%",
                 (self.capital_gbp / STARTING_CAPITAL_GBP - 1) * 100)
        log.info("-" * 60)
        log.info("  Total trades:  %d", self.total_trades)
        log.info("  Win rate:      %.1f%%", self.win_rate)
        if self.in_trade:
            log.info("  Open trade:  %s", self.current_trade.summary())
        else:
            log.info("  Open trade:  None -- watching for setup")
        log.info("=" * 60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log.info("Stanley self-test (Gold)")
    stanley = PaperTraderGold()
    stanley.open_trade("LONG", 4155.63, 1.3376, "OVERLAP")
    stanley.monitor_trade(4165.0, 1.3376)
    stanley.monitor_trade(4190.0, 1.3376)
    result = stanley.close_trade(4185.0, "TAKE_PROFIT", 1.3376)
    log.info("Trade result: %s", result.summary() if result else "None")
    stanley.print_status()
    log.info("Stanley self-test complete.")
