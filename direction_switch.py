"""
direction_switch.py -- WITH / AGAINST direction switch for the Benchmark Desk.

Live reload: the engine re-reads the mode from logs/direction_switch.json on every
tick, so the switch can be flipped from the dashboard (or BenchmarkRoundTable) with
NO restart. Written atomically (temp + os.replace) so a reader can never see a
half-written file. Defaults to WITH; state persists across restarts.

  WITH    -- trade WITH the 3-timeframe SSL signal direction
  AGAINST -- trade AGAINST it (contrarian: flip the execution direction)
"""
import json
import os
from datetime import datetime, timezone

_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "direction_switch.json")
VALID_MODES = ("WITH", "AGAINST")


def get_mode() -> str:
    """Current direction mode, re-read fresh from disk on every call (live reload).
    Defaults to WITH and creates the file with WITH on first use."""
    try:
        with open(_FILE, encoding="utf-8") as f:
            mode = str(json.load(f).get("mode", "WITH")).upper()
        return mode if mode in VALID_MODES else "WITH"
    except Exception:
        try:
            set_mode("WITH", set_by="default")
        except Exception:
            pass
        return "WITH"


def get_state() -> dict:
    """Full switch state {mode, set_at, set_by} for the dashboard / RoundTable."""
    try:
        with open(_FILE, encoding="utf-8") as f:
            d = json.load(f)
        mode = str(d.get("mode", "WITH")).upper()
        if mode not in VALID_MODES:
            mode = "WITH"
        return {"mode": mode, "set_at": d.get("set_at", ""), "set_by": d.get("set_by", "")}
    except Exception:
        return {"mode": "WITH", "set_at": "", "set_by": ""}


def set_mode(mode, set_by="Nick") -> dict:
    """Persist a new direction mode atomically. Returns the written state."""
    mode = str(mode).upper()
    if mode not in VALID_MODES:
        raise ValueError("invalid direction mode: %s (want WITH|AGAINST)" % mode)
    os.makedirs(os.path.dirname(_FILE), exist_ok=True)
    data = {
        "mode": mode,
        "set_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "set_by": set_by or "Nick",
    }
    tmp = _FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _FILE)   # atomic swap -- readers never see a partial file
    return data


def flip(direction: str) -> str:
    """LONG <-> SHORT."""
    return "SHORT" if direction == "LONG" else "LONG"
