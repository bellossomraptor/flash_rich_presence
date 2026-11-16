"""Flash Rich Presence

Shows a Discord Rich Presence status while the Adobe Flash Player standalone
projector (flashplayer_32_sa.exe) is running. Windows only.

Settings live in config.json next to this script (or next to the exe).
"""

import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psutil
from pypresence import Presence
from pypresence.exceptions import InvalidID

__version__ = "1.0.0"


def app_dir():
    """Folder holding the script, or the exe when built with PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "flash_presence.log"

DEFAULTS = {
    "client_id": "1572001957625860116",
    "process_prefix": "flashplayer",
    "poll_interval": 5,
    "min_update_gap": 15,
    "reconnect_delay": 15,
    "show_window_title": False,
    "large_image": "flash_logo",
    "large_text": "Adobe Flash Player 32",
    "details": "Running Flash Player 32",
    "state": "Flash Player Standalone",
}

# --- Logging (file only, so it works with no console / pythonw / --noconsole) ---
handler = RotatingFileHandler(LOG_PATH, maxBytes=500_000, backupCount=2, encoding="utf-8")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[handler],
)
log = logging.getLogger("flash_presence")


def load_config():
    """Merge config.json over the defaults and sanity-check the values."""
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            if not isinstance(user_cfg, dict):
                raise ValueError("config.json must contain a JSON object")
            unknown = set(user_cfg) - set(DEFAULTS)
            if unknown:
                log.warning("Ignoring unknown config keys: %s", ", ".join(sorted(unknown)))
            cfg.update({k: v for k, v in user_cfg.items() if k in DEFAULTS})
        except (OSError, ValueError) as e:
            log.error("Could not read config.json (%s); using defaults", e)
    else:
        log.info("No config.json found; using defaults")

    cfg["client_id"] = str(cfg["client_id"]).strip()
    if not (cfg["client_id"].isdigit() and 17 <= len(cfg["client_id"]) <= 20):
        log.critical("client_id must be the numeric Application ID from the Developer Portal")
        raise SystemExit(1)

    cfg["process_prefix"] = str(cfg["process_prefix"]).lower()
    for key, minimum in (("poll_interval", 1), ("min_update_gap", 15), ("reconnect_delay", 5)):
        try:
            cfg[key] = max(minimum, float(cfg[key]))
        except (TypeError, ValueError):
            log.warning("Bad value for %s; using default", key)
            cfg[key] = DEFAULTS[key]
    return cfg


def find_process(prefix):
    """Return the first running process whose name starts with prefix."""
    for p in psutil.process_iter(["name"]):
        try:
            name = p.info["name"]
            if name and name.lower().startswith(prefix):
                return p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def make_title_reader():
    """Return a function pid -> window title, or None if pywin32 is unavailable."""
    try:
        import win32gui
        import win32process
    except ImportError:
        log.warning("show_window_title is on but pywin32 is not installed; disabling it")
        return None

    def get_window_title(pid):
        titles = []

        def callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                _, win_pid = win32process.GetWindowThreadProcessId(hwnd)
                if win_pid == pid:
                    title = win32gui.GetWindowText(hwnd)
                    if title:
                        titles.append(title)

        win32gui.EnumWindows(callback, None)
        return titles[0] if titles else None

    return get_window_title


def connect(cfg):
    """Block until a connection to the local Discord client is established."""
    while True:
        try:
            rpc = Presence(cfg["client_id"])
            rpc.connect()
            log.info("Connected to Discord")
            return rpc
        except InvalidID:
            log.critical("Invalid client_id; check the Developer Portal")
            raise SystemExit(1)
        except Exception as e:  # Discord not running, pipe errors, etc.
            log.warning("Discord unavailable (%s: %s); retrying in %ss",
                        type(e).__name__, e, cfg["reconnect_delay"])
            time.sleep(cfg["reconnect_delay"])


def main():
    log.info("Flash Rich Presence %s started", __version__)
    cfg = load_config()
    read_title = make_title_reader() if cfg["show_window_title"] else None

    rpc = connect(cfg)
    start_time = None
    last_details = None
    last_update = 0.0

    try:
        while True:
            try:
                proc = find_process(cfg["process_prefix"])

                if proc:
                    if start_time is None:
                        start_time = int(time.time())
                        log.info("Detected %s", proc.info["name"])

                    details = cfg["details"]
                    if read_title:
                        try:
                            title = read_title(proc.pid)
                            if title:
                                details = f"Playing: {title}"
                        except Exception as e:
                            log.warning("Could not read window title: %s", e)
                    details = details[:128]

                    now = time.time()
                    # Update only on change, and never faster than the rate limit
                    if details != last_details and now - last_update >= cfg["min_update_gap"]:
                        kwargs = dict(
                            details=details,
                            state=cfg["state"],
                            start=start_time,
                            large_text=cfg["large_text"],
                        )
                        if cfg["large_image"]:
                            kwargs["large_image"] = cfg["large_image"]
                        rpc.update(**kwargs)
                        last_details = details
                        last_update = now
                        log.info("Presence updated: %s", details)

                elif start_time is not None:
                    rpc.clear()
                    log.info("Process closed; presence cleared")
                    start_time = None
                    last_details = None
                    last_update = time.time()

            except Exception as e:
                # Covers PipeClosed, BrokenPipeError, ConnectionResetError, other OSErrors,
                # and pypresence errors, typically from Discord restarting.
                log.warning("Presence error (%s: %s); reconnecting", type(e).__name__, e)
                try:
                    rpc.close()
                except Exception:
                    pass
                rpc = connect(cfg)
                last_details = None   # force a refresh after reconnect
                last_update = 0.0

            time.sleep(cfg["poll_interval"])

    except KeyboardInterrupt:
        pass
    finally:
        try:
            rpc.clear()
            rpc.close()
        except Exception:
            pass
        log.info("Shut down")


if __name__ == "__main__":
    main()
