"""Decky backend for BonkScanner Deck.

Runs as root (``"flags": ["root"]`` in plugin.json): reading another process's
memory and creating a ``/dev/uinput`` keyboard both require it. The reroll loop
itself lives in ``py_modules/bonk_deck.py`` and runs on a worker thread; the
frontend polls :meth:`Plugin.get_status` and gets a ``bonk_finished`` event.
"""

import asyncio
import json
import os
import re
import sys
import threading
import time
import urllib.request

import decky

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "py_modules"))
import bonk_deck  # noqa: E402
import deck_buttons  # noqa: E402

SETTINGS_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
# Every evaluated map, for reroll odds. Same columns as BonkScanner for Windows.
MAP_ROLLS_FILE = os.path.join(decky.DECKY_PLUGIN_RUNTIME_DIR, "map_rolls.csv")
DEFAULT_SETTINGS = {
    "moai": 4,
    "shady": 0,
    "sm_total": 0,
    "micro": 2,
    "boss": 0,
    "challenges": 0,
    "magnet_max": -1,  # -1 = no limit
    "pause_on_found": True,
    "skip_current": False,
    "start_delay": 3,
    "hotkey": "R4",
    "near_miss_enabled": False,
    "near_miss_stat": "Moais",
    "near_miss_minimum": 8,
    "near_miss_seconds": 10,
}
INT_LIMITS = {
    "moai": (0, 20), "shady": (0, 20), "sm_total": (0, 40), "micro": (0, 2),
    "boss": (0, 20), "challenges": (0, 20), "magnet_max": (-1, 20), "start_delay": (0, 10),
    "near_miss_minimum": (1, 30), "near_miss_seconds": (3, 60),
}
LOG_LINES = 6

# Updates come only from this repository's published releases. The plugin runs
# as root, so the source is fixed here rather than read from anywhere else, and
# installing is always the player's choice: Decky shows its own confirmation
# and checks the zip's SHA-256 before replacing anything.
UPDATE_REPO = "lightview/bonkscanner-deck"
UPDATE_ASSET = "bonkscanner-deck.zip"
UPDATE_CHECK_TTL = 15 * 60


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version or "")[:3])


def _http_get(url: str, *, limit: int = 1_000_000) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": "bonkscanner-deck-updater",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read(limit)


def fetch_latest_release() -> dict:
    release = json.loads(_http_get(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"))
    tag = str(release.get("tag_name", ""))
    expected_url = f"https://github.com/{UPDATE_REPO}/releases/download/{tag}/{UPDATE_ASSET}"
    assets = {asset.get("name"): asset for asset in release.get("assets", [])}
    zip_asset = assets.get(UPDATE_ASSET)
    if not tag or zip_asset is None or zip_asset.get("browser_download_url") != expected_url:
        raise ValueError("The latest release has no plugin zip.")
    digest = str(zip_asset.get("digest") or "")
    sha256 = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
    if not sha256 and f"{UPDATE_ASSET}.sha256" in assets:
        sha256 = _http_get(assets[f"{UPDATE_ASSET}.sha256"]["browser_download_url"], limit=200).decode().split()[0]
    if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
        raise ValueError("The latest release has no SHA-256 for its zip.")
    return {
        "version": tag.lstrip("v"),
        "url": expected_url,
        "sha256": sha256,
        "page": release.get("html_url") or f"https://github.com/{UPDATE_REPO}/releases",
    }


def _sanitize(raw: dict) -> dict:
    settings = dict(DEFAULT_SETTINGS)
    for key, default in DEFAULT_SETTINGS.items():
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(default, bool):
            settings[key] = bool(value)
        elif key == "hotkey":
            if value in deck_buttons.HOTKEYS:
                settings[key] = value
        elif key == "near_miss_stat":
            if value in bonk_deck.NEAR_MISS_STATS:
                settings[key] = value
        else:
            low, high = INT_LIMITS[key]
            try:
                settings[key] = min(high, max(low, int(value)))
            except (TypeError, ValueError):
                pass
    return settings


class Plugin:
    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.scanner = None
        self.thread = None
        # The hotkey thread and the frontend can both start a scan.
        self.start_lock = threading.Lock()
        self.log_lines = []
        self.last_status = {"state": "idle", "message": "", "rerolls": 0, "elapsed": 0.0,
                            "last": None, "found": None, "target": ""}
        self.settings = self._load_settings()
        self.update_info = None
        self.update_checked_at = 0.0
        self.hotkey_listener = deck_buttons.BackButtonListener(
            get_hotkey=lambda: self.settings["hotkey"],
            on_press=self._on_hotkey,
            log=decky.logger.info,
        )
        self.hotkey_listener.start()
        decky.logger.info("BonkScanner Deck loaded")

    async def _unload(self):
        self.hotkey_listener.stop()
        self._stop_and_join()
        decky.logger.info("BonkScanner Deck unloaded")

    async def _uninstall(self):
        self.hotkey_listener.stop()
        self._stop_and_join()

    # -- settings ---------------------------------------------------------------
    def _load_settings(self) -> dict:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return _sanitize(json.load(f))
        except (OSError, ValueError):
            return dict(DEFAULT_SETTINGS)

    async def get_settings(self) -> dict:
        return self.settings

    async def save_settings(self, settings: dict) -> dict:
        self.settings = _sanitize(settings or {})
        try:
            os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
        except OSError as exc:
            decky.logger.error(f"Could not save settings: {exc}")
        return self.settings

    # -- updates ----------------------------------------------------------------
    async def check_update(self, force: bool = False) -> dict:
        """Compare this build with the latest GitHub release (cached 15 min)."""
        current = decky.DECKY_PLUGIN_VERSION
        now = time.monotonic()
        if force or self.update_info is None or now - self.update_checked_at > UPDATE_CHECK_TTL:
            try:
                latest = await asyncio.get_running_loop().run_in_executor(None, fetch_latest_release)
                self.update_info = {"ok": True, **latest}
            except Exception as exc:
                decky.logger.info(f"Update check failed: {exc}")
                self.update_info = {"ok": False, "error": str(exc)}
            self.update_checked_at = now
        info = dict(self.update_info, current=current)
        info["available"] = bool(
            info.get("ok") and _version_tuple(info["version"]) > _version_tuple(current)
        )
        return info

    # -- scanning ---------------------------------------------------------------
    def _log(self, message: str) -> None:
        decky.logger.info(message)
        self.log_lines = (self.log_lines + [message])[-LOG_LINES:]

    def _template(self) -> dict:
        s = self.settings
        template = {key: s[key] for key in ("moai", "shady", "sm_total", "micro", "boss", "challenges")}
        if s["magnet_max"] >= 0:
            template["magnet_max"] = s["magnet_max"]
        return template

    def _is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _emit(self, event: str, *args) -> None:
        asyncio.run_coroutine_threadsafe(decky.emit(event, *args), self.loop)

    def _on_hotkey(self) -> None:
        """Back-button press in game: toggle rerolling, no menu to close first."""
        if self._is_running():
            self.scanner.stop()
            self._emit("bonk_hotkey", "stopping", self.settings["hotkey"])
        else:
            self._start(start_delay=0.0)
            self._emit("bonk_hotkey", "started", self.settings["hotkey"])

    async def start_scan(self) -> dict:
        self._start(start_delay=float(self.settings["start_delay"]))
        return await self.get_status()

    def _start(self, start_delay: float) -> None:
        with self.start_lock:
            if not self._is_running():
                self._start_locked(start_delay)

    def _start_locked(self, start_delay: float) -> None:
        s = self.settings
        self.log_lines = []
        self.scanner = bonk_deck.Scanner(
            self._template(),
            pause=s["pause_on_found"],
            skip_current=s["skip_current"],
            start_delay=start_delay,
            roll_log_path=MAP_ROLLS_FILE,
            near_miss={
                "stat": s["near_miss_stat"],
                "minimum": s["near_miss_minimum"],
                "seconds": s["near_miss_seconds"],
            } if s["near_miss_enabled"] else None,
            wait_for_keep=deck_buttons.wait_for_any_button,
            on_event=lambda name, status: self._emit(f"bonk_{name}", status),
            log_fn=self._log,
        )
        self.thread = threading.Thread(target=self._worker, args=(self.scanner,),
                                       name="BonkScannerDeck", daemon=True)
        self.thread.start()

    def _worker(self, scanner) -> None:
        try:
            result = scanner.run()
        except Exception as exc:  # never leave the UI stuck on "scanning"
            decky.logger.exception("Scanner crashed")
            result = dict(scanner.snapshot(), state="error", message=f"Unexpected error: {exc}")
        self.last_status = result
        self._emit("bonk_finished", result)

    async def stop_scan(self) -> dict:
        if self.scanner is not None:
            self.scanner.stop()
        return await self.get_status()

    def _stop_and_join(self) -> None:
        if self.scanner is not None:
            self.scanner.stop()
        if self.thread is not None:
            self.thread.join(timeout=5)

    async def get_status(self) -> dict:
        if self._is_running():
            status = self.scanner.snapshot()
            status["running"] = True
        else:
            status = dict(self.last_status)
            status["running"] = False
        status["log"] = list(self.log_lines)
        return status
