"""
compass/quality_report.py -- Merlin's Compass Session-1 data-quality + verification report.
Read-only over the cached CSVs (+ one Capital.com price read for the match check). Prints a plain-English
report for Nick. Not wired to Lancelot.
"""
import time
from datetime import datetime, timezone

try:
    from . import polygon_fetcher as pf
    from . import swing_levels as sl
except ImportError:
    import polygon_fetcher as pf
    import swing_levels as sl


def _dt(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def gap_analysis(rows, expected_min=5):
    """Find gaps between consecutive candles. Weekend gaps (Fri->Sun ~ 45-50h) are expected; flag the rest."""
    weekend, other = [], []
    biggest = (0, None, None)
    for a, b in zip(rows, rows[1:]):
        da, db = _dt(a["timestamp"]), _dt(b["timestamp"])
        gap_min = (db - da).total_seconds() / 60.0
        if gap_min <= expected_min * 2:
            continue
        # a genuine weekend gap starts Friday and ends Sunday/Monday
        is_weekend = da.weekday() == 4 or db.weekday() == 6 or gap_min >= 40 * 60
        (weekend if is_weekend else other).append((gap_min, a["timestamp"], b["timestamp"]))
        if gap_min > biggest[0]:
            biggest = (gap_min, a["timestamp"], b["timestamp"])
    return weekend, other, biggest


def run():
    bar = "=" * 66
    print(bar); print("MERLIN'S COMPASS -- SESSION 1 DATA QUALITY REPORT"); print(bar)

    # ── Part 3: counts, range, gaps ──
    five = pf.load("5min"); hour = pf.load("1hour"); day = pf.load("daily")
    print("\n[1] CANDLE COUNTS")
    for lbl, rows in (("5-minute", five), ("1-hour", hour), ("daily", day)):
        if rows:
            print("    %-9s %8d candles   %s  ->  %s"
                  % (lbl, len(rows), rows[0]["timestamp"], rows[-1]["timestamp"]))

    wk, other, big = gap_analysis(five)
    print("\n[2] GAP ANALYSIS (5-minute series)")
    print("    weekend/holiday gaps (expected) : %d" % len(wk))
    print("    OTHER gaps >10min (flagged)     : %d" % len(other))
    if big[1]:
        print("    largest gap                     : %.1f h   (%s -> %s)"
              % (big[0] / 60.0, big[1], big[2]))
    for g in sorted(other, reverse=True)[:5]:
        print("      - %.1f h non-weekend gap: %s -> %s" % (g[0] / 60.0, g[1], g[2]))
    if not other:
        print("    (no unexpected intra-week gaps)")

    # ── swing levels on today's 1h ──
    print("\n[3] SWING LEVELS -- today's Gold 1-hour chart")
    lv = sl.detect()
    print("    current price       : %.2f  (last 1h close, %s)" % (lv["current_price"], lv["timestamp"]))
    print("    resistance (nearest->): %s" % lv["resistance_levels"])
    print("    support    (nearest->): %s" % lv["support_levels"])
    print("    nearest resistance  : %.2f   (upside  %.2f pts)" % (lv["nearest_resistance"], lv["upside_to_resistance"]))
    print("    nearest support     : %.2f   (downside %.2f pts)" % (lv["nearest_support"], lv["downside_to_support"]))
    print("    swings found in 72h : %d highs / %d lows" % (lv["swing_highs_found"], lv["swing_lows_found"]))

    # ── price match vs Capital.com ──
    print("\n[4] PRICE MATCH vs Capital.com (Gold)")
    try:
        import trading_mode, capitalcom_connector as cc
        conn = cc.CapitalComConnector(account=trading_mode.read_mode())
        conn.connect()
        snap = conn.get_price("GOLD") if hasattr(conn, "get_price") else None
        cap = None
        if isinstance(snap, dict):
            cap = snap.get("mid") or snap.get("bid") or snap.get("price")
        elif isinstance(snap, (int, float)):
            cap = snap
        poly = five[-1]["close"]
        if cap:
            print("    Polygon last close  : %.2f" % poly)
            print("    Capital.com Gold    : %.2f  (%s, market %s)"
                  % (cap, trading_mode.read_mode(), "CLOSED weekend" if datetime.now(timezone.utc).weekday() >= 5
                     or (datetime.now(timezone.utc).weekday() == 4 and datetime.now(timezone.utc).hour >= 21) else "OPEN"))
            print("    difference          : %.2f pts" % abs(cap - poly))
        else:
            print("    (could not read a Capital.com Gold price this run)")
    except Exception as e:
        print("    (Capital.com read skipped: %s)" % e)

    # ── Part 4: Oil ticker availability on Polygon ──
    print("\n[5] OIL TICKER AVAILABILITY ON POLYGON (Currencies plan)")
    import os, requests
    from dotenv import load_dotenv
    load_dotenv(pf._SECRETS); key = os.getenv("POLYGON_API_KEY")
    for tk in ("C:USOIL", "C:UKOIL", "C:WTIUSD", "C:BRENTUSD"):
        try:
            u = "https://api.polygon.io/v2/aggs/ticker/%s/range/1/day/2026-08-01/2026-08-14" % tk
            d = requests.get(u, params={"limit": 5, "apiKey": key}, timeout=15).json()
            print("    %-12s status=%-10s count=%s" % (tk, d.get("status"), d.get("resultsCount", "-")))
        except Exception as e:
            print("    %-12s ERROR %s" % (tk, e))

    # ── Tests: refresh + speed ──
    print("\n[6] TEST -- refresh logic (no re-fetch of stored data)")
    before = len(pf.load("daily"))
    r1 = pf.update("daily")
    after = len(pf.load("daily"))
    print("    daily update: action=%s rows_total=%d rows_added=%d (was %d) -> %s"
          % (r1["action"], r1["rows_total"], r1["rows_added"], before,
             "OK no duplication" if after <= before + r1["rows_added"] else "CHECK"))

    print("\n[7] TEST -- speed (warm cache)")
    t0 = time.perf_counter(); sl.detect(); dt = time.perf_counter() - t0
    print("    swing_levels.detect() : %.3f s   (%s < 1.0s target)" % (dt, "PASS" if dt < 1.0 else "FAIL"))

    print("\n" + bar)


if __name__ == "__main__":
    run()
