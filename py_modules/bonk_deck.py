#!/usr/bin/env python3
"""BonkScanner for Steam Deck (Linux / Proton) -- auto-reroll engine and CLI.

A self-contained port of the Windows BonkScanner scanner loop
(https://github.com/ALuiell/BonkScanner, GPL-3.0):

* memory is read through ``/proc/<pid>/mem`` instead of pymem;
* the restart key is held through a virtual ``/dev/uinput`` keyboard instead of
  the ``keyboard`` package;
* map readiness (``GameDataClient.wait_for_map_ready``) and template matching
  (``core.logic.find_matching_template``) are ported unchanged in behaviour.

The Decky plugin imports :class:`Scanner`; the same file also runs on its own.
Standard library only. Needs root:

    sudo python3 bonk_deck.py --probe              # read the current map once
    sudo python3 bonk_deck.py --test-key           # hold R once after 5 s
    sudo python3 bonk_deck.py --moai 4 --micro 2   # auto-reroll until matched
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import re
import struct
import sys
import threading
import time
from typing import Callable

PROCESS_NAME = "megabonk.exe"
MODULE_NAME = "gameassembly.dll"

# ---------------------------------------------------------------------------
# Offsets -- copied from BonkScanner src/infra/memory/game_data_client.py
# ---------------------------------------------------------------------------
TYPE_INFO_OFFSET = 0x2FB5E68
MAP_CONTROLLER_TYPE_INFO_OFFSET = 0x2F58E08
MAP_GENERATION_CONTROLLER_TYPE_INFO_OFFSET = 0x2F59000
MY_TIME_TYPE_INFO_OFFSET = 0x2F62398
MY_TIME_PAUSED_OFFSET = 0x0
CLASS_STATIC_FIELDS_OFFSET = 0xB8
MAP_CONTROLLER_CURRENT_MAP_OFFSET = 0x10
MAP_CONTROLLER_CURRENT_STAGE_OFFSET = 0x18
MAP_CONTROLLER_INDEX_OFFSET = 0x08
MAP_CONTROLLER_RESETING_OFFSET = 0x21
MAP_GENERATION_IS_GENERATING_OFFSET = 0x10
MAP_GENERATION_MAP_SEED_OFFSET = 0x2C
DICT_ENTRIES_OFFSET = 0x18
DICT_COUNT_OFFSET = 0x20
DICT_VERSION_OFFSET = 0x2C
ENTRY_BASE_OFFSET = 0x20
ENTRY_SIZE = 0x18
ENTRY_KEY_OFFSET = 0x8
ENTRY_VALUE_OFFSET = 0x10
CONTAINER_MAX_OFFSET = 0x10
CONTAINER_CURRENT_OFFSET = 0x14
MAX_DICT_ENTRIES = 4096

STAT_LABELS = (
    "Bald Heads", "Boss Curses", "Challenges", "Charge Shrines", "Chests",
    "Greed Shrines", "Magnet Shrines", "Microwaves", "Moais", "Pots", "Shady Guy",
)
EXPECTED_READY_STATS = frozenset(STAT_LABELS) - {"Bald Heads"}
READY_POLL_INTERVAL = 0.01
READY_STATS_STABILITY_DURATION = 0.025

DEFAULT_RESET_HOLD_DURATION = 0.4
RESET_HOLD_SAFETY_MARGIN = 0.05


class MemoryReadError(Exception):
    pass


class ProcessNotFoundError(Exception):
    pass


class ScanError(Exception):
    """A condition the user has to fix; the message is shown as-is."""


class ScanStopped(Exception):
    """Raised inside the loop when :meth:`Scanner.stop` was requested."""


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ---------------------------------------------------------------------------
# Process memory (replaces infra/memory/reader.py)
# ---------------------------------------------------------------------------
def _comm(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/comm", "rb") as f:
            return f.read().decode(errors="replace").strip().lower()
    except OSError:
        return ""


def _process_matches(pid: str) -> bool:
    if _comm(pid) == PROCESS_NAME:
        return True
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = f.read().split(b"\0")
    except OSError:
        return False
    for arg in args:
        name = re.split(r"[\\/]", arg.decode(errors="replace"))[-1].lower()
        if name == PROCESS_NAME:
            return True
    return False


def _module_base(pid: str) -> int | None:
    """Base of GameAssembly.dll as Wine mapped it (offset-0 file mapping)."""
    best = None
    try:
        with open(f"/proc/{pid}/maps", "r", errors="replace") as f:
            for line in f:
                parts = line.split(maxsplit=5)
                if len(parts) < 6:
                    continue
                path = parts[5].strip()
                if os.path.basename(path).lower() != MODULE_NAME:
                    continue
                start = int(parts[0].split("-")[0], 16)
                offset = int(parts[2], 16)
                if offset == 0 and (best is None or start < best):
                    best = start
    except OSError:
        return None
    return best


NATIVE_BUILD_MESSAGE = (
    "The native Linux build of Megabonk is running. BonkScanner needs the Windows "
    "build: in Steam open Megabonk > Properties > Compatibility, enable "
    "'Force the use of a specific Steam Play compatibility tool', pick Proton "
    "and restart the game."
)


class ProcessMemory:
    def __init__(self) -> None:
        pids = [pid for pid in os.listdir("/proc") if pid.isdigit()]
        candidates = [pid for pid in pids if int(pid) != os.getpid() and _process_matches(pid)]
        if not candidates:
            if any(_comm(pid) == "megabonk.x86_64" for pid in pids):
                raise ScanError(NATIVE_BUILD_MESSAGE)
            raise ProcessNotFoundError("Megabonk is not running.")
        for pid in candidates:
            base = _module_base(pid)
            if base:
                self.pid = int(pid)
                self.base = base
                break
        else:
            raise ProcessNotFoundError("Megabonk found, waiting for it to finish loading.")
        try:
            self._fd = os.open(f"/proc/{self.pid}/mem", os.O_RDONLY)
        except PermissionError as exc:
            raise ScanError("Permission denied reading game memory (root is required).") from exc

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass

    def read_bytes(self, address: int, size: int) -> bytes:
        if address <= 0:
            raise MemoryReadError(f"Invalid address 0x{address:X}.")
        try:
            data = os.pread(self._fd, size, address)
        except OSError as exc:
            raise MemoryReadError(f"Failed to read {size} bytes at 0x{address:X}: {exc}") from exc
        if len(data) != size:
            raise MemoryReadError(f"Short read at 0x{address:X}.")
        return data

    def read_ptr(self, address: int) -> int:
        return struct.unpack("<Q", self.read_bytes(address, 8))[0]

    def read_i32(self, address: int) -> int:
        return struct.unpack("<i", self.read_bytes(address, 4))[0]

    def read_u8(self, address: int) -> int:
        return self.read_bytes(address, 1)[0]

    def read_mono_string(self, address: int, max_length: int = 512) -> str | None:
        if not address:
            return None
        try:
            length = self.read_i32(address + 0x10)
            if length < 0 or length > max_length:
                return None
            if length == 0:
                return ""
            return self.read_bytes(address + 0x14, length * 2).decode("utf-16-le")
        except (MemoryReadError, UnicodeDecodeError):
            return None

    def is_alive(self) -> bool:
        return os.path.exists(f"/proc/{self.pid}")


# ---------------------------------------------------------------------------
# Game data (ported from GameDataClient)
# ---------------------------------------------------------------------------
class MapState:
    __slots__ = ("is_generating", "map_seed", "map_ptr", "stage_ptr", "is_resetting", "stage_index")

    def __init__(self, is_generating=False, map_seed=None, map_ptr=0, stage_ptr=0,
                 is_resetting=False, stage_index=None) -> None:
        self.is_generating = is_generating
        self.map_seed = map_seed
        self.map_ptr = map_ptr
        self.stage_ptr = stage_ptr
        self.is_resetting = is_resetting
        self.stage_index = stage_index

    @property
    def has_loaded_map(self) -> bool:
        return bool(self.map_ptr and self.stage_ptr)

    def __repr__(self) -> str:
        return (f"MapState(generating={self.is_generating}, seed={self.map_seed}, "
                f"map=0x{self.map_ptr:X}, stage=0x{self.stage_ptr:X}, "
                f"resetting={self.is_resetting}, stage_index={self.stage_index})")


class GameClient:
    def __init__(self, memory: ProcessMemory) -> None:
        self.memory = memory
        self.last_ready_state: MapState | None = None
        self._last_revision = None

    def _static_fields(self, type_info_offset: int) -> int:
        m = self.memory
        class_ptr = m.read_ptr(m.base + type_info_offset)
        if not class_ptr:
            raise MemoryReadError(f"Type info not initialized at 0x{type_info_offset:X}.")
        fields = m.read_ptr(class_ptr + CLASS_STATIC_FIELDS_OFFSET)
        if not fields:
            raise MemoryReadError(f"Static fields not initialized at 0x{type_info_offset:X}.")
        return fields

    def get_map_state(self) -> MapState:
        m = self.memory
        gen = self._static_fields(MAP_GENERATION_CONTROLLER_TYPE_INFO_OFFSET)
        ctl = self._static_fields(MAP_CONTROLLER_TYPE_INFO_OFFSET)
        return MapState(
            is_generating=m.read_u8(gen + MAP_GENERATION_IS_GENERATING_OFFSET) != 0,
            map_seed=m.read_i32(gen + MAP_GENERATION_MAP_SEED_OFFSET),
            map_ptr=m.read_ptr(ctl + MAP_CONTROLLER_CURRENT_MAP_OFFSET),
            stage_ptr=m.read_ptr(ctl + MAP_CONTROLLER_CURRENT_STAGE_OFFSET),
            is_resetting=m.read_u8(ctl + MAP_CONTROLLER_RESETING_OFFSET) != 0,
            stage_index=m.read_i32(ctl + MAP_CONTROLLER_INDEX_OFFSET),
        )

    def is_paused(self) -> bool:
        fields = self._static_fields(MY_TIME_TYPE_INFO_OFFSET)
        return self.memory.read_u8(fields + MY_TIME_PAUSED_OFFSET) != 0

    def get_map_stats(self) -> dict[str, tuple[int, int]]:
        """``label -> (current, max)`` for the known interactables."""
        m = self.memory
        self._last_revision = None
        stats: dict[str, tuple[int, int]] = {}
        class_ptr = m.read_ptr(m.base + TYPE_INFO_OFFSET)
        if not class_ptr:
            return stats
        fields = m.read_ptr(class_ptr + CLASS_STATIC_FIELDS_OFFSET)
        if not fields:
            return stats
        dictionary = m.read_ptr(fields)
        if not dictionary:
            return stats
        entries = m.read_ptr(dictionary + DICT_ENTRIES_OFFSET)
        count = m.read_i32(dictionary + DICT_COUNT_OFFSET)
        if count < 0 or count > MAX_DICT_ENTRIES:
            raise MemoryReadError(f"Interactables dictionary count is invalid: {count}")
        if not entries:
            if count:
                raise MemoryReadError("Interactables dictionary has entries but a null array.")
            return stats
        version = m.read_i32(dictionary + DICT_VERSION_OFFSET)
        for index in range(count):
            entry = entries + ENTRY_BASE_OFFSET + index * ENTRY_SIZE
            key_ptr = m.read_ptr(entry + ENTRY_KEY_OFFSET)
            value_ptr = m.read_ptr(entry + ENTRY_VALUE_OFFSET)
            if not key_ptr or not value_ptr:
                continue
            label = m.read_mono_string(key_ptr)
            if not label:
                raise MemoryReadError(f"Interactables label unreadable at 0x{key_ptr:X}.")
            if label in STAT_LABELS:
                stats[label] = (
                    m.read_i32(value_ptr + CONTAINER_CURRENT_OFFSET),
                    m.read_i32(value_ptr + CONTAINER_MAX_OFFSET),
                )
        revision = (entries, count, version)
        after = (
            m.read_ptr(dictionary + DICT_ENTRIES_OFFSET),
            m.read_i32(dictionary + DICT_COUNT_OFFSET),
            m.read_i32(dictionary + DICT_VERSION_OFFSET),
        )
        if after != revision:
            raise MemoryReadError("Interactables dictionary changed during the read.")
        self._last_revision = revision
        return stats

    @staticmethod
    def _normalize(stats):
        return {label: stats.get(label, (0, 0)) for label in EXPECTED_READY_STATS}

    def wait_for_map_ready(self, previous_state: MapState | None = None, previous_stats=None,
                           require_change: bool = True, timeout: float = 10.0,
                           abort: Callable[[], bool] | None = None):
        """Port of ``GameDataClient.wait_for_map_ready`` -- fails closed."""
        self.last_ready_state = None
        deadline = time.monotonic() + timeout
        generation_seen = False
        map_change_seen = False
        base_seed = previous_state.map_seed if previous_state else None
        base_map = previous_state.map_ptr if previous_state and previous_state.map_ptr else None
        base_stage = previous_state.stage_ptr if previous_state and previous_state.stage_ptr else None
        has_identity = any(v is not None for v in (base_seed, base_map, base_stage))
        base_stats = self._normalize(previous_stats) if previous_stats is not None else None
        stable_stats = ready_raw = stable_rev = stable_since = None
        last_state = MapState()
        last_error = None

        while time.monotonic() < deadline:
            if abort is not None and abort():
                raise ScanStopped()
            try:
                last_state = self.get_map_state()
                generation_seen = generation_seen or last_state.is_generating
                map_change_seen = map_change_seen or generation_seen
                if base_seed is not None and last_state.map_seed != base_seed:
                    map_change_seen = True
                if base_map is not None and last_state.map_ptr and last_state.map_ptr != base_map:
                    map_change_seen = True
                if base_stage is not None and last_state.stage_ptr and last_state.stage_ptr != base_stage:
                    map_change_seen = True

                lifecycle_ready = (not last_state.is_generating and not last_state.is_resetting
                                   and last_state.has_loaded_map)
                change_ready = not require_change or generation_seen or map_change_seen

                if not lifecycle_ready or (not change_ready and has_identity):
                    stable_stats = ready_raw = stable_rev = stable_since = None
                else:
                    stats = self.get_map_stats()
                    revision = self._last_revision
                    ready = self._normalize(stats)
                    if base_stats is None:
                        base_stats = ready
                    elif ready != base_stats and not has_identity:
                        map_change_seen = change_ready = True

                    if change_ready and stats:
                        now = time.monotonic()
                        if stats != stable_stats or revision != stable_rev or ready_raw is None:
                            stable_stats, ready_raw, stable_rev, stable_since = dict(stats), stats, revision, now
                        elif now - stable_since >= READY_STATS_STABILITY_DURATION:
                            self.last_ready_state = last_state
                            return ready_raw
                    else:
                        stable_stats = ready_raw = stable_rev = stable_since = None
            except MemoryReadError as exc:
                last_error = exc
                stable_stats = ready_raw = stable_rev = stable_since = None
            time.sleep(READY_POLL_INTERVAL)

        text = (f"Timed out waiting for map readiness after {timeout:.1f}s. Last state: {last_state}. "
                f"change_seen={map_change_seen}, generation_seen={generation_seen}.")
        if last_error is not None:
            text += f" Last memory error: {last_error}"
        raise TimeoutError(text)


# ---------------------------------------------------------------------------
# Template matching (ported from core/logic.py)
# ---------------------------------------------------------------------------
COUNTERS = (
    ("shady", "Shady Guy"),
    ("moai", "Moais"),
    ("micro", "Microwaves"),
    ("boss", "Boss Curses"),
    ("magnet", "Magnet Shrines"),
    ("challenges", "Challenges"),
)


def template_microwaves(stats: dict[str, int]) -> int:
    value = stats.get("Microwaves")
    if stats.get("Chests", 0) >= 69:  # Graveyard-style maps report raw counts
        return max(0, int(value or 0))
    if value is None or value < 1:
        return 1
    return 2 if value > 2 else value


def template_matches(stats: dict[str, int], template: dict) -> bool:
    values = {key: stats.get(label, 0) for key, label in COUNTERS}
    values["micro"] = template_microwaves(stats)
    if stats.get("Shady Guy", 0) + stats.get("Moais", 0) < (template.get("sm_total") or 0):
        return False
    for key, _label in COUNTERS:
        if values[key] < (template.get(key) or 0):
            return False
        maximum = template.get(f"{key}_max")
        if maximum is not None and values[key] > maximum:
            return False
    return True


def describe_template(template: dict) -> str:
    parts = []
    if template.get("sm_total"):
        parts.append(f"S+M>={template['sm_total']}")
    for key, label in COUNTERS:
        if template.get(key):
            parts.append(f"{label}>={template[key]}")
        if template.get(f"{key}_max") is not None:
            parts.append(f"{label}<={template[f'{key}_max']}")
    return ", ".join(parts) or "(no conditions -- matches any map)"


def summarize_stats(stats: dict[str, int]) -> dict[str, int]:
    return {
        "moai": stats.get("Moais", 0),
        "shady": stats.get("Shady Guy", 0),
        "micro": stats.get("Microwaves", 0),
        "boss": stats.get("Boss Curses", 0),
        "magnet": stats.get("Magnet Shrines", 0),
        "challenges": stats.get("Challenges", 0),
    }


def format_stats(stats: dict[str, int]) -> str:
    return (f"Moai {stats.get('Moais', 0)} | Shady {stats.get('Shady Guy', 0)} | "
            f"Micro {stats.get('Microwaves', 0)} | Boss {stats.get('Boss Curses', 0)} | "
            f"Magnet {stats.get('Magnet Shrines', 0)} | Chall {stats.get('Challenges', 0)}")


# ---------------------------------------------------------------------------
# Virtual keyboard (replaces infra/keyboard_run_control.py)
# ---------------------------------------------------------------------------
EV_SYN, EV_KEY, SYN_REPORT = 0x00, 0x01, 0
UI_SET_EVBIT, UI_SET_KEYBIT = 0x40045564, 0x40045565
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502
KEY_CODES = {
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50, "space": 57, "esc": 1,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
}


class VirtualKeyboard:
    def __init__(self) -> None:
        try:
            self._fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except PermissionError as exc:
            raise ScanError("Permission denied opening /dev/uinput (root is required).") from exc
        fcntl.ioctl(self._fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self._fd, UI_SET_EVBIT, EV_SYN)
        for code in KEY_CODES.values():
            fcntl.ioctl(self._fd, UI_SET_KEYBIT, code)
        # Legacy struct uinput_user_dev: name, input_id, ff_effects_max, 4 x absinfo[64].
        device = struct.pack("<80sHHHHI256i", b"BonkScanner Virtual Keyboard",
                             0x03, 0x1209, 0xB0E1, 1, 0, *([0] * 256))
        os.write(self._fd, device)
        fcntl.ioctl(self._fd, UI_DEV_CREATE)
        self._held: int | None = None
        time.sleep(1.0)  # let the compositor pick the new device up

    def _emit(self, ev_type: int, code: int, value: int) -> None:
        os.write(self._fd, struct.pack("llHHi", 0, 0, ev_type, code, value))

    def _key(self, code: int, down: bool) -> None:
        self._emit(EV_KEY, code, 1 if down else 0)
        self._emit(EV_SYN, SYN_REPORT, 0)
        self._held = code if down else None

    def hold(self, key: str, seconds: float) -> None:
        code = KEY_CODES[key]
        self._key(code, True)
        try:
            time.sleep(seconds)
        finally:
            self._key(code, False)

    def close(self) -> None:
        if self._held is not None:
            try:
                self._key(self._held, False)
            except OSError:
                pass
        try:
            fcntl.ioctl(self._fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self._fd)


# ---------------------------------------------------------------------------
# Game settings: quick_reset_time from the Proton prefix
# ---------------------------------------------------------------------------
CONFIG_SUFFIX = ("pfx/drive_c/users/steamuser/AppData/LocalLow/Ved/Megabonk/Saves/"
                 "LocalDir/config.json")
LIBRARY_ROOTS = (
    "/home/*/.local/share/Steam/steamapps",
    "/home/*/.steam/steam/steamapps",
    "/run/media/*/steamapps",
    "/run/media/*/*/steamapps",
    "/run/media/*/*/*/steamapps",
)


def read_quick_reset_time() -> tuple[float | None, str | None]:
    for root in LIBRARY_ROOTS:
        for path in glob.glob(f"{root}/compatdata/*/{CONFIG_SUFFIX}"):
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    text = f.read()
            except OSError:
                continue
            value = None
            try:
                value = json.loads(text).get("cfGameSettings", {}).get("quick_reset_time")
            except (ValueError, AttributeError):
                match = re.search(r'"quick_reset_time"\s*:\s*([0-9.]+)', text)
                value = match.group(1) if match else None
            try:
                return float(value), path
            except (TypeError, ValueError):
                return None, path
    return None, None


def resolve_hold_duration(log_fn: Callable[[str], None] = log) -> float:
    quick_reset, path = read_quick_reset_time()
    if quick_reset is not None:
        log_fn(f"[*] quick_reset_time {quick_reset} from {path}")
        return round(quick_reset + RESET_HOLD_SAFETY_MARGIN, 2)
    log_fn(f"[*] Game config not found; holding the reset key {DEFAULT_RESET_HOLD_DURATION}s.")
    return DEFAULT_RESET_HOLD_DURATION


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------
class Scanner:
    """The reroll loop. ``run()`` blocks; ``stop()`` may be called from any thread.

    ``status`` is a plain dict snapshot for the UI:
    ``state`` is one of idle, waiting_game, starting, scanning, waiting_unpause,
    found, already_matches, stopped, limit, error.
    """

    def __init__(self, template: dict, *, key: str = "r", hold: float | None = None,
                 max_rerolls: int = 0, force: bool = False, pause: bool = True,
                 skip_current: bool = False, start_delay: float = 0.0,
                 log_fn: Callable[[str], None] = log,
                 on_status: Callable[[dict], None] | None = None) -> None:
        self.template = template
        self.key = key
        self.hold = hold
        self.max_rerolls = max_rerolls
        self.force = force
        self.pause = pause
        self.skip_current = skip_current
        self.start_delay = start_delay
        self._log = log_fn
        self._on_status = on_status
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._started_at: float | None = None
        self.status: dict = {
            "state": "idle", "message": "", "rerolls": 0, "elapsed": 0.0,
            "last": None, "found": None, "target": describe_template(template),
        }

    # -- control -------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            status = dict(self.status)
        if self._started_at is not None and status["state"] in ("scanning", "waiting_unpause"):
            status["elapsed"] = round(time.monotonic() - self._started_at, 1)
        return status

    def _set(self, **changes) -> None:
        with self._lock:
            self.status.update(changes)
        if self._on_status is not None:
            self._on_status(self.snapshot())

    def _sleep(self, seconds: float) -> None:
        if self._stop.wait(seconds):
            raise ScanStopped()

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise ScanStopped()

    # -- loop ----------------------------------------------------------------
    def _connect(self) -> ProcessMemory:
        announced = False
        while True:
            self._check_stop()
            try:
                memory = ProcessMemory()
            except ProcessNotFoundError as exc:
                if not announced:
                    self._log(f"[WAIT] {exc}")
                    self._set(state="waiting_game", message=str(exc))
                    announced = True
                self._sleep(1.0)
                continue
            self._log(f"[+] Connected: pid {memory.pid}, GameAssembly.dll base 0x{memory.base:X}")
            if memory.read_bytes(memory.base, 2) != b"MZ":
                self._log("[!] Unexpected module header -- base address may be wrong.")
            return memory

    def _ensure_unpaused(self, client: GameClient, keyboard: VirtualKeyboard) -> None:
        """Leave the pause menu once with Esc; R does nothing while paused."""
        if not client.is_paused():
            return
        self._log("[*] Game is paused -- pressing Esc to resume.")
        keyboard.hold("esc", 0.05)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if not client.is_paused():
                self._log("[*] Resumed.")
                self._sleep(0.2)  # let the menu close before the first reset
                return
            self._sleep(0.05)
        self._wait_until_unpaused(client)

    def _wait_until_unpaused(self, client: GameClient) -> None:
        """Never send the reset key into a menu: wait for the player to resume."""
        if not client.is_paused():
            return
        self._log("[WAIT] Game is paused -- resume it to continue rerolling.")
        self._set(state="waiting_unpause", message="Game is paused - resume it to continue.")
        while client.is_paused():
            self._sleep(0.1)
        self._log("[*] Resumed.")
        self._set(state="scanning", message="")
        self._sleep(0.2)

    def run(self) -> dict:
        memory = keyboard = None
        rerolls = 0
        try:
            hold = self.hold if self.hold is not None else resolve_hold_duration(self._log)
            memory = self._connect()
            client = GameClient(memory)
            state = client.get_map_state()
            if state.stage_index not in (None, 0) and not self.force:
                raise ScanError("The run is already past stage 1; refusing to reset it.")
            keyboard = VirtualKeyboard()
            self._log(f"[*] Target: {describe_template(self.template)}")
            self._log(f"[*] Reset key '{self.key}', hold {hold:.2f}s.")

            delay = self.start_delay
            while delay > 0:
                self._set(state="starting", message=f"Close the menu - starting in {int(delay + 0.99)}...")
                step = min(1.0, delay)
                self._sleep(step)
                delay -= step

            self._started_at = time.monotonic()
            self._set(state="scanning", message="")
            is_first, last_state, last_stats = True, None, None
            while True:
                try:
                    raw = client.wait_for_map_ready(previous_state=last_state, previous_stats=last_stats,
                                                    require_change=not is_first, timeout=10.0,
                                                    abort=self._stop.is_set)
                except TimeoutError as exc:
                    if not memory.is_alive():
                        raise ScanError("The game process exited.")
                    self._log("[-] Map took too long to load; re-evaluating the current map.")
                    self._log(f"    {exc}")
                    is_first, last_state, last_stats = True, None, None
                    continue

                is_first = False
                last_state = client.last_ready_state or client.get_map_state()
                last_stats = raw
                stats = {label: maximum for label, (_current, maximum) in raw.items()}
                summary = summarize_stats(stats)
                matched = template_matches(stats, self.template)

                if matched and rerolls == 0 and not self.skip_current:
                    # Evaluated while still in the pause menu if the player was
                    # there: an already-matching map is left exactly as it is.
                    self._log(f"[$$$] The current map already matches: {format_stats(stats)}")
                    if self.pause and not client.is_paused():
                        keyboard.hold("esc", 0.05)
                    self._set(state="already_matches", last=summary, found=summary,
                              message="The current map already matches.")
                    return self.snapshot()
                if matched and rerolls > 0:
                    elapsed = time.monotonic() - self._started_at
                    self._log(f"[$$$] TARGET MAP FOUND after {rerolls} rerolls ({elapsed:.0f}s): "
                              f"{format_stats(stats)}")
                    if self.pause:
                        # Same as the Windows scanner's handle_confirmed_target_window.
                        keyboard.hold("esc", 0.05)
                        self._log("[*] Game paused (Esc).")
                    self._set(state="found", last=summary, found=summary, elapsed=round(elapsed, 1),
                              message=f"Found after {rerolls} rerolls.")
                    return self.snapshot()

                self._log(f"#{rerolls:<5} {format_stats(stats)}")
                self._set(last=summary, rerolls=rerolls)

                if last_state.stage_index not in (None, 0) and not self.force:
                    raise ScanError("The run moved past stage 1; stopping instead of resetting it.")
                if self.max_rerolls and rerolls >= self.max_rerolls:
                    self._log(f"[*] Reached the limit of {self.max_rerolls} rerolls.")
                    self._set(state="limit", message=f"Stopped after {rerolls} rerolls (limit).")
                    return self.snapshot()
                if rerolls == 0:
                    self._ensure_unpaused(client, keyboard)
                else:
                    self._wait_until_unpaused(client)
                self._check_stop()
                keyboard.hold(self.key, hold)
                rerolls += 1
                self._set(rerolls=rerolls)
        except ScanStopped:
            self._log(f"[*] Stopped after {rerolls} rerolls.")
            self._set(state="stopped", message=f"Stopped after {rerolls} rerolls.")
        except ScanError as exc:
            self._log(f"[-] {exc}")
            self._set(state="error", message=str(exc))
        except MemoryReadError as exc:
            self._log(f"[-] Lost connection to the game: {exc}")
            self._set(state="error", message="Lost connection to the game.")
        finally:
            if keyboard is not None:
                keyboard.close()
            if memory is not None:
                memory.close()
        return self.snapshot()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def run_probe() -> None:
    try:
        memory = ProcessMemory()
    except (ProcessNotFoundError, ScanError) as exc:
        raise SystemExit(f"[-] {exc}")
    log(f"[+] Connected: pid {memory.pid}, GameAssembly.dll base 0x{memory.base:X}")
    client = GameClient(memory)
    try:
        log(f"State: {client.get_map_state()}, paused={client.is_paused()}")
        raw = client.get_map_stats()
        if not raw:
            log("Interactables dictionary is empty -- start a run (be on the map) and probe again.")
        for label in STAT_LABELS:
            if label in raw:
                current, maximum = raw[label]
                log(f"  {label:15} {maximum:4}   (used {current})")
    finally:
        memory.close()


def run_test_key(key: str, hold: float) -> None:
    keyboard = VirtualKeyboard()
    try:
        for i in range(5, 0, -1):
            log(f"Switch to the game -- holding '{key}' for {hold:.2f}s in {i}...")
            time.sleep(1.0)
        keyboard.hold(key, hold)
        log("Done. Did the run restart?")
    finally:
        keyboard.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="BonkScanner auto-reroll for Steam Deck (Proton).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--probe", action="store_true", help="read the current map once and exit")
    mode.add_argument("--test-key", action="store_true", help="hold the reset key once after 5 s")
    parser.add_argument("--sm", type=int, default=0, help="minimum Shady + Moai")
    for key, label in COUNTERS:
        parser.add_argument(f"--{key}", type=int, default=0, help=f"minimum {label}")
        parser.add_argument(f"--{key}-max", type=int, default=None, help=f"maximum {label}")
    parser.add_argument("--key", default="r", choices=sorted(KEY_CODES), help="reset key (default r)")
    parser.add_argument("--hold", type=float, default=None,
                        help="seconds to hold the reset key (default: game's quick_reset_time + 0.05)")
    parser.add_argument("--max-rerolls", type=int, default=0, help="stop after N rerolls (0 = no limit)")
    parser.add_argument("--force", action="store_true", help="allow resetting a run past stage 1")
    parser.add_argument("--no-pause", action="store_true", help="do not press Esc when the target is found")
    parser.add_argument("--skip-current", action="store_true",
                        help="reroll even if the current map already matches")
    args = parser.parse_args()

    if sys.platform != "linux":
        raise SystemExit("This script is for Linux / Steam Deck.")
    if args.probe:
        run_probe()
        return
    if args.test_key:
        run_test_key(args.key, args.hold if args.hold is not None else resolve_hold_duration())
        return

    template = {"sm_total": args.sm}
    for key, _label in COUNTERS:
        template[key] = getattr(args, key)
        template[f"{key}_max"] = getattr(args, f"{key}_max")
    scanner = Scanner(template, key=args.key, hold=args.hold, max_rerolls=args.max_rerolls,
                      force=args.force, pause=not args.no_pause, skip_current=args.skip_current)
    try:
        scanner.run()
    except KeyboardInterrupt:
        # run()'s finally has already released the key and closed the devices.
        log("[*] Stopped by user.")


if __name__ == "__main__":
    main()
