#!/usr/bin/env python3
"""
mi_temp_reporter.py
────────────────────────────────────────────────────────────────────────────────
Robust hourly temperature/humidity reporter for Xiaomi LYWSD03MMC sensors
running custom ATC/pvvx firmware (passive mode) via MiTemperature2.

Designed for unattended deployment on a Raspberry Pi Zero W at a remote location.

Configuration is done via a config file (see DEFAULT_CONFIG below) or by
environment variables with the MI_TEMP_ prefix.
────────────────────────────────────────────────────────────────────────────────
"""

import argparse
import configparser
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = {
    # Path to MiTemperature2.py
    "mitemp_script": "/home/pi/git/MiTemperature2/MiTemperature2.py",

    # Path to MiTemperature2 device list
    "devicelist_file": "sensors.ini",

    # How often to POST data to the API, in seconds (default: 3600 = 1 hour)
    "interval_seconds": "3600",

    # How long (seconds) MiTemperature2 is allowed to run before we kill it
    # Should be shorter than interval_seconds.
    "scan_timeout_seconds": "60",

    # HTTP API endpoint to POST JSON data to
    "api_url": "http://your-api-endpoint/data",

    # Number of times to retry a failed HTTP POST before giving up
    "http_retries": "3",

    # Seconds between HTTP retry attempts
    "http_retry_delay": "10",

    # Bluetooth interface index (0 = hci0)
    "bt_interface": "0",

    # Watchdog timer passed to MiTemperature2 (seconds without BLE packet
    # before re-enabling scan).
    "watchdog_timer": "5",

    # Log file path (empty = log to stdout only)
    "log_file": "/var/log/mi_temp_reporter.log",

    # Maximum log file size in bytes before rotation
    "log_max_bytes": "5242880",  # 5 MB

    # Number of rotated log backups to keep
    "log_backup_count": "3",
}

CONFIG_FILE_PATHS = [
    "/etc/mi_temp_reporter.conf",
    str(Path.home() / ".config" / "mi_temp_reporter.conf"),
    "mi_temp_reporter.conf",
]

def setup_logging(cfg: dict) -> logging.Logger:
    logger = logging.getLogger("mi_temp_reporter")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Rotating file handler (if configured)
    log_file = cfg.get("log_file", "").strip()
    if log_file:
        try:
            fh = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=int(cfg.get("log_max_bytes", DEFAULT_CONFIG["log_max_bytes"])),
                backupCount=int(cfg.get("log_backup_count", DEFAULT_CONFIG["log_backup_count"])),
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError as exc:
            logger.warning("Cannot open log file %s: %s — logging to stdout only.", log_file, exc)

    return logger


def load_config(config_file: str | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)

    # 1. Read from config file
    parser = configparser.ConfigParser()
    candidates = ([config_file] if config_file else []) + CONFIG_FILE_PATHS
    for path in candidates:
        if path and Path(path).is_file():
            parser.read(path)
            if "mi_temp" in parser:
                cfg.update(dict(parser["mi_temp"]))
            break

    # 2. Override with environment variables (MI_TEMP_<KEY>)
    for key in DEFAULT_CONFIG:
        env_val = os.environ.get(f"MI_TEMP_{key.upper()}")
        if env_val is not None:
            cfg[key] = env_val

    return cfg


def build_mitemp_command(cfg: dict) -> list[str]:
    """Build the MiTemperature2.py command with a shell callback."""
    cmd = [
        sys.executable,
        cfg["mitemp_script"],
        "--callback", "sendToFile.sh",
        "--watchdogtimer", str(cfg["watchdog_timer"]),
        "--interface", str(cfg["bt_interface"]),
        "--devicelistfile", cfg["devicelist_file"],
        "--onlydevicelist",
        "--round",
        "--battery",
    ]
    return cmd


def collect_reading(cfg: dict, logger: logging.Logger) -> dict | None:
    """
    Run MiTemperature2 for scan_timeout_seconds and collect the first (or most
    recent) reading via a temp file callback.  Returns a dict with at least
    temperature and humidity keys, or None on failure.
    """
    import tempfile

    scan_timeout = int(cfg["scan_timeout_seconds"])
    readings: list[dict] = []
    readings_lock = threading.Lock()
    data_path = "data.txt" # sendToFile.sh writes here

    proc = None
    try:
        logger.info("Starting BLE scan (timeout %d s) …", scan_timeout)
        cmd = build_mitemp_command(cfg)
        logger.debug("Command: %s", " ".join(cmd))

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,  # own process group so we can kill tree
        )

        deadline = time.monotonic() + scan_timeout
        last_reading: dict | None = None

        while time.monotonic() < deadline:
            # Poll process health
            if proc.poll() is not None:
                logger.warning("MiTemperature2 exited early (code %d).", proc.returncode)
                break

            try:
                with open(data_path) as df:
                    lines = [l.strip() for l in df if l.strip()]
                if lines:
                    for line in lines:
                        logger.debug("Raw callback line: %s", line)
                        try:
                            reading = parse_reading(line)
                            if isinstance(reading.get("temperature"), str):
                                last_reading = reading
                        except json.JSONDecodeError:
                            pass
            except OSError:
                pass

            time.sleep(1)

        if last_reading:
            logger.info(
                "Reading: temp=%.1f°C  hum=%.0f%%  sensor=%s",
                last_reading.get("temperature", "?"),
                last_reading.get("humidity", "?"),
                last_reading.get("sensorname", "?"),
            )
        else:
            logger.warning("No valid reading collected within scan window.")

        return last_reading

    except FileNotFoundError:
        logger.error("MiTemperature2 script not found at: %s", cfg["mitemp_script"])
        return None
    except Exception as exc:
        logger.exception("Unexpected error running MiTemperature2: %s", exc)
        return None
    finally:
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.wait(timeout=3)
            except ProcessLookupError:
                pass
            except Exception as kill_exc:
                logger.warning("Error killing MiTemperature2: %s", kill_exc)
        # cleanup data file
        try:
            os.remove(data_path)
        except OSError:
            pass

def parse_reading(line: str) -> dict:
    parts = line.split()
    if len(parts) != 7:
        raise ValueError(f"Unexpected callback line format: {line}")
    sensorname, temperature, humidity, voltage, batteryLevel, timestamp = parts
    return {
        "sensorname": sensorname,
        "temperature": temperature,
        "humidity": humidity,
        "voltage": voltage,
        "timestamp": timestamp,
        "batteryLevel": batteryLevel,
    }

def post_reading(reading: dict, cfg: dict, logger: logging.Logger) -> bool:
    """POST the reading as JSON to the configured API endpoint.  Returns True on success."""
    url = cfg["api_url"].strip()
    retries = int(cfg.get("http_retries", DEFAULT_CONFIG["http_retries"]))
    retry_delay = int(cfg.get("http_retry_delay", DEFAULT_CONFIG["http_retry_delay"]))

    payload = {
        **reading,
        "reportedAt": datetime.now(timezone.utc).isoformat(),
    }
    body = json.dumps(payload).encode("utf-8")

    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    for attempt in range(1, retries + 2):  # +1 for the initial try
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                status = resp.status
                if 200 <= status < 300:
                    logger.info("POST success (HTTP %d) to %s", status, url)
                    return True
                else:
                    logger.warning("POST returned HTTP %d (attempt %d/%d)", status, attempt, retries + 1)
        except urllib.error.HTTPError as exc:
            logger.warning("HTTP error %d on attempt %d/%d: %s", exc.code, attempt, retries + 1, exc.reason)
        except urllib.error.URLError as exc:
            logger.warning("URL error on attempt %d/%d: %s", attempt, retries + 1, exc.reason)
        except OSError as exc:
            logger.warning("Network error on attempt %d/%d: %s", attempt, retries + 1, exc)

        if attempt <= retries:
            logger.info("Retrying in %d s …", retry_delay)
            time.sleep(retry_delay)

    logger.error("All %d POST attempts failed for URL: %s", retries + 1, url)
    return False


class GracefulShutdown:
    """Catches SIGTERM / SIGINT and allows the main loop to exit cleanly."""
    def __init__(self):
        self._stop = threading.Event()
        signal.signal(signal.SIGTERM, self._handler)
        signal.signal(signal.SIGINT, self._handler)

    def _handler(self, signum, frame):
        self._stop.set()

    @property
    def requested(self) -> bool:
        return self._stop.is_set()

    def wait(self, timeout: float) -> bool:
        """Sleep for timeout seconds or until stop is requested. Returns True if stop was requested."""
        return self._stop.wait(timeout=timeout)


def run(cfg: dict, logger: logging.Logger) -> None:
    interval = int(cfg["interval_seconds"])
    shutdown = GracefulShutdown()

    logger.info(
        "mi_temp_reporter started | interval=%ds  api=%s",
        interval,
        cfg["api_url"],
    )

    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 10  # after this many failures in a row, log a loud warning

    while not shutdown.requested:
        cycle_start = time.monotonic()

        try:
            reading = collect_reading(cfg, logger)
            if reading:
                success = post_reading(reading, cfg, logger)
                if success:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
            else:
                consecutive_failures += 1
                logger.error("No reading obtained; skipping POST (consecutive failures: %d)", consecutive_failures)

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.critical(
                    "⚠️  %d consecutive failures — check sensor, BLE, and network connectivity.",
                    consecutive_failures,
                )

        except Exception as exc:
            # Belt-and-suspenders: catch anything so the loop never dies.
            consecutive_failures += 1
            logger.exception("Unhandled exception in main loop (will continue): %s", exc)

        # Sleep for the remainder of the interval
        elapsed = time.monotonic() - cycle_start
        sleep_time = max(0, interval - elapsed)
        logger.debug("Cycle took %.1f s; sleeping %.0f s until next reading.", elapsed, sleep_time)

        if shutdown.wait(timeout=sleep_time):
            break

    logger.info("Shutdown requested — exiting cleanly.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Hourly Xiaomi MiTemperature2 → HTTP API reporter",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument(
        "--config", "-c",
        metavar="FILE",
        default=None,
        help="Path to configuration file (INI format, [mi_temp] section)",
    )
    ap.add_argument(
        "--once",
        action="store_true",
        help="Read the sensor once, POST the result, then exit (useful for testing)",
    )
    args = ap.parse_args()

    cfg = load_config(args.config)
    logger = setup_logging(cfg)

    if args.once:
        logger.info("Running in --once mode.")
        reading = collect_reading(cfg, logger)
        if reading:
            post_reading(reading, cfg, logger)
        else:
            logger.error("No reading obtained.")
            sys.exit(1)
    else:
        run(cfg, logger)


if __name__ == "__main__":
    main()