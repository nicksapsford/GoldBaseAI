"""
watchdog_cryptobenchmark.py -- Galahad for GoldBase (Benchmark Desk).
Launches and supervises main_goldbase.py: restarts it on unexpected exit,
respects the logs/shutdown.flag (a dashboard-requested stop is NOT restarted),
and backs off after repeated rapid failures. All times UTC.
"""
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SHUTDOWN_FLAG = LOG_DIR / "shutdown.flag"
TARGET = BASE_DIR / "main_goldbase.py"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s [Galahad] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logging.Formatter.converter = time.gmtime
log = logging.getLogger("GoldBase.Watchdog")

MAX_RAPID_FAILURES = 5      # failures within RAPID_WINDOW before a long backoff
RAPID_WINDOW       = 120    # seconds
BACKOFF_SECONDS    = 300    # cool-down after too many rapid failures


def main() -> None:
    log.info("Galahad supervising %s", TARGET.name)
    SHUTDOWN_FLAG.unlink(missing_ok=True)
    recent_failures = []

    while True:
        if SHUTDOWN_FLAG.exists():
            log.info("Shutdown flag present -- not starting engine. Consuming flag and exiting.")
            SHUTDOWN_FLAG.unlink(missing_ok=True)
            return

        start = time.monotonic()
        log.info("Starting engine: %s", TARGET.name)
        try:
            proc = subprocess.Popen([sys.executable, str(TARGET)], cwd=str(BASE_DIR))
            rc = proc.wait()
        except Exception as exc:
            log.error("Failed to launch engine: %s", exc)
            rc = -1
        ran_for = time.monotonic() - start

        # A dashboard-requested shutdown leaves the flag -> honour it, don't restart.
        if SHUTDOWN_FLAG.exists():
            log.info("Engine exited on shutdown flag (rc=%s) -- clean stop, not restarting.", rc)
            SHUTDOWN_FLAG.unlink(missing_ok=True)
            return

        log.warning("Engine exited (rc=%s) after %.0fs -- restarting.", rc, ran_for)
        now = time.monotonic()
        recent_failures = [t for t in recent_failures if (now - t) < RAPID_WINDOW]
        recent_failures.append(now)
        if len(recent_failures) >= MAX_RAPID_FAILURES:
            log.error("%d failures within %ds -- backing off %ds.",
                      len(recent_failures), RAPID_WINDOW, BACKOFF_SECONDS)
            time.sleep(BACKOFF_SECONDS)
            recent_failures = []
        else:
            time.sleep(5)


if __name__ == "__main__":
    main()
