# GoldBase A.I.

Part of the **Albion Benchmark Desk** — a parallel scientific baseline for the
original Albion Trading Desk. GoldBase trades Gold (XAU/USD) on **pure
Lancelot signals with no AI overlay**, so its P&L can be compared like-for-like
against the original GoldTrader.

- **Port:** 5033 · **Instrument:** Gold XAU/USD (Capital.com) · **Balance:** £1,000
- **Template:** GoldTrader v1.2.7 · **Paper trading only** · **Session:** 22:00–21:00 UTC

## Decision engine (the whole thing)

Every 5-minute candle:

1. **Lancelot pre-checks** must all pass — identical to GoldTrader (`pre_checks_gold`, copied verbatim, confirmed pure). This includes the **session-aware SHORT filter** (Asian session applies a tighter RSI 60/40 conviction threshold), retained as-is.
2. **3-timeframe SSL agreement** — Daily + 1h + 5m SSL must all point the same way. That is the direction signal.
3. **Direction switch** decides execution:
   - `WITH` — trade the SSL direction.
   - `AGAINST` — trade the opposite (contrarian). Lancelot always validates the *signal* direction; only the executed direction flips.

Exits are pure risk management: **30pt trailing stop / 50pt take profit / Profit Protection Ladder (Variant 2)** — via Stanley's `monitor_trade()`.

**Stripped vs GoldTrader:** no Arthur (AI), no Morgan (confidence), no Guinevere (news), no phantom logging, no confidence thresholds.

Parameters: 0.3pt spread, bidirectional, switch default **WITH**.

## The direction switch (WITH / AGAINST)
One switch, **live reload** — re-read from `logs/direction_switch.json` every tick; flip from the dashboard or BenchmarkRoundTable with **no restart**. Default WITH; persists; atomic write.

## Running
```
python dashboard_goldbase.py     # port 5033 (switch + status)
python watchdog_goldbase.py      # supervises main_goldbase.py
```

Appears automatically on **BenchmarkRoundTable** (port 5030) once running.

All times UTC.
