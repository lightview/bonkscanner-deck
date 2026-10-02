"""Read the Steam Deck's back buttons straight from the controller's hidraw node.

The game never sees L4/R4/L5/R5 -- Steam Input owns them -- but any number of
readers may open the controller's hidraw device, so this works no matter how
the buttons are mapped in Steam. Report layout as in the kernel's hid-steam.c
(Steam Deck input report 0x09), confirmed on hardware:

    L4 = byte 13 bit 1, R4 = byte 13 bit 2, L5 = byte 9 bit 7, R5 = byte 10 bit 0
"""

from __future__ import annotations

import glob
import os
import select
import threading
import time
from typing import Callable

VALVE_DECK_HID_ID = "000028DE:00001205"
DECK_INPUT_REPORT = 0x09
BUTTON_BITS = {
    "L4": (13, 1),
    "R4": (13, 2),
    "L5": (9, 7),
    "R5": (10, 0),
}
HOTKEYS = ("off", "L4", "R4", "L5", "R5", "L4+R4", "L5+R5")
TOGGLE_COOLDOWN = 0.6


def _buttons(hotkey: str) -> list[tuple[int, int]]:
    return [BUTTON_BITS[name] for name in hotkey.split("+") if name in BUTTON_BITS]


def _deck_hidraw_nodes() -> list[str]:
    nodes = []
    for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(path, "device", "uevent"), "r") as f:
                if VALVE_DECK_HID_ID in f.read().upper():
                    nodes.append("/dev/" + os.path.basename(path))
        except OSError:
            continue
    return nodes


def _open_input_node() -> int | None:
    """The controller exposes several hidraw nodes; only one streams input."""
    for node in _deck_hidraw_nodes():
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            try:
                report = os.read(fd, 64)
            except OSError:
                break
            if len(report) >= 16 and report[2] == DECK_INPUT_REPORT:
                return fd
        os.close(fd)
    return None


class BackButtonListener:
    """Calls ``on_press`` once per press of the configured button combination."""

    def __init__(self, get_hotkey: Callable[[], str], on_press: Callable[[], None],
                 log: Callable[[str], None]) -> None:
        self._get_hotkey = get_hotkey
        self._on_press = on_press
        self._log = log
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="BonkBackButtons", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        fd = None
        announced_missing = False
        was_down = False
        last_toggle = 0.0
        while not self._stop.is_set():
            buttons = _buttons(self._get_hotkey())
            if not buttons:
                if fd is not None:
                    os.close(fd)
                    fd = None
                self._stop.wait(0.5)
                continue
            if fd is None:
                fd = _open_input_node()
                if fd is None:
                    if not announced_missing:
                        self._log("[!] Steam Deck controller not found; back-button hotkey disabled.")
                        announced_missing = True
                    self._stop.wait(3.0)
                    continue
                announced_missing = False
                was_down = False
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
                if not ready:
                    continue
                report = os.read(fd, 64)
            except OSError:
                # Suspend/resume re-enumerates the controller: reopen it.
                os.close(fd)
                fd = None
                self._stop.wait(1.0)
                continue
            if len(report) < 16 or report[2] != DECK_INPUT_REPORT:
                continue
            down = all(report[byte] & (1 << bit) for byte, bit in buttons)
            now = time.monotonic()
            if down and not was_down and now - last_toggle >= TOGGLE_COOLDOWN:
                last_toggle = now
                try:
                    self._on_press()
                except Exception as exc:  # a UI hiccup must not kill the listener
                    self._log(f"[-] Hotkey action failed: {exc}")
            was_down = down
        if fd is not None:
            os.close(fd)


# Every physical button, by report byte -- hid-steam.c's Deck layout. Steam
# (9.5) and Quick Access (14.2) are left out: they open Steam's own overlays,
# so pressing them is not an answer to our prompt. Touch-only bits (trackpad
# and stick capacitive sensors) are left out too, or a resting thumb would
# count as a press.
ANY_BUTTON_MASK = {
    8: 0xFF,   # R2, L2, R1, L1, Y, B, X, A
    9: 0xDF,   # D-pad, View, Menu, L5 (not Steam)
    10: 0x47,  # R5, left/right pad click, L3
    11: 0x04,  # R3
    13: 0x06,  # L4, R4
}


def _pressed_bits(report: bytes) -> dict[int, int]:
    return {byte: report[byte] & mask for byte, mask in ANY_BUTTON_MASK.items()}


def wait_for_any_button(seconds: float, should_abort: Callable[[], bool]) -> bool:
    """True when a button goes down within ``seconds``; False on timeout.

    Buttons already held when the wait starts count only once released and
    pressed again. Without a readable controller this just waits it out.
    """
    deadline = time.monotonic() + seconds
    fd = _open_input_node()
    if fd is None:
        while time.monotonic() < deadline and not should_abort():
            time.sleep(0.05)
        return False
    try:
        held: dict[int, int] | None = None
        while time.monotonic() < deadline:
            if should_abort():
                return False
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                continue
            try:
                report = os.read(fd, 64)
            except OSError:
                return False
            if len(report) < 16 or report[2] != DECK_INPUT_REPORT:
                continue
            bits = _pressed_bits(report)
            if held is None:
                held = bits
                continue
            if any(bits[byte] & ~held[byte] for byte in bits):
                return True
            held = {byte: held[byte] & bits[byte] for byte in bits}  # released keys re-arm
        return False
    finally:
        os.close(fd)
