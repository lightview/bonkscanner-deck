"""Drives ``deck_overlay.py`` from the plugin backend.

The backend runs as root inside Decky's bundled Python, which has no GTK; the
overlay runs as the desktop user under the system Python on Steam's X display.
They talk through one small JSON file that this side replaces atomically.

Card lifecycle, matching BonkScanner for Windows:

* while a hunt runs: live progress (rerolls, time, closest map, odds);
* near miss: the held map and a countdown;
* found: stays while the game is paused, then counts down 5 s once it runs
  again (the run timer advancing is the witness); "kept" counts down at once,
  since the player is clearly there;
* otherwise hidden -- and the overlay process is ended, so the Steam
  performance overlay, which shares gamescope's single overlay slot, returns.
"""

from __future__ import annotations

import json
import math
import os
import pwd
import subprocess
import threading
import time
from html import escape
from typing import Callable

STATE_PATH = "/tmp/bonkscanner-deck-overlay.json"
OVERLAY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deck_overlay.py")
SYSTEM_PYTHON = "/usr/bin/python3"
STEAM_DISPLAY = ":0"
LINGER_SECONDS = 5.0
WRITE_INTERVAL = 0.25

MET, UNMET, MUTED, ACCENT, FOUND, NEAR = "#6FD38A", "#F2A65A", "#9EA2B3", "#5B8DEF", "#F6C453", "#F2A65A"


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds or 0)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _span(text: str, color: str | None = None, *, size: str | None = None, bold: bool = False) -> str:
    attrs = []
    if color:
        attrs.append(f'foreground="{color}"')
    if size:
        attrs.append(f'size="{size}"')
    if bold:
        attrs.append('weight="bold"')
    return f"<span {' '.join(attrs)}>{escape(text)}</span>" if attrs else escape(text)


def _map_text(summary: dict | None) -> str:
    if not summary:
        return ""
    return (f"Moai {summary.get('moai', 0)} · Shady {summary.get('shady', 0)} · Micro {summary.get('micro', 0)} · "
            f"Boss {summary.get('boss', 0)} · Magnet {summary.get('magnet', 0)}")


def _rate(status: dict) -> str:
    rerolls, elapsed = status.get("rerolls") or 0, status.get("elapsed") or 0
    return f" · {elapsed / rerolls:.1f} s each" if rerolls and elapsed else ""


def _odds_line(odds: dict | None) -> str | None:
    if not odds:
        return None
    if not odds.get("reliable"):
        return _span(f"Odds: collecting data ({odds.get('maps', 0)} maps recorded)", MUTED, size="small")
    return _span(
        f"Odds ~1 in {round(odds['one_in']):,} · 50% by {format_duration(odds['p50_seconds'])} · "
        f"90% by {format_duration(odds['p90_seconds'])} ({odds['hits']}/{odds['maps']} maps)",
        MUTED, size="small",
    )


def hunting_card(status: dict) -> tuple[str, list[str]]:
    lines = [
        _span("Hunting", bold=True, size="large") + "  " + _span(status.get("target") or "", ACCENT, bold=True),
        f"<b>{status.get('rerolls', 0)}</b> rerolls · <b>{format_duration(status.get('elapsed', 0))}</b>"
        + _span(_rate(status), MUTED, size="small"),
    ]
    closest = status.get("closest")
    if closest and closest.get("requirements"):
        parts = [
            _span(f"{r['label']} ", MET if r["met"] else UNMET)
            + f"<span foreground=\"{MET if r['met'] else UNMET}\"><b>{r['value']}</b>/{escape(str(r['target']))}</span>"
            for r in closest["requirements"]
        ]
        lines.append("Closest: " + " · ".join(parts) + _span(f"  (#{closest.get('at', 0)})", MUTED, size="small"))
    if status.get("last"):
        lines.append(_span("Last: " + _map_text(status["last"]), MUTED, size="small"))
    odds = _odds_line(status.get("odds"))
    if odds:
        lines.append(odds)
    return ACCENT, lines


def near_miss_card(status: dict, remaining: int) -> tuple[str, list[str]]:
    near = status.get("near_miss") or {}
    return NEAR, [
        _span("Near miss!", NEAR, bold=True, size="large") + "  "
        + _span(f"{near.get('stat', '')} {near.get('value', '')}", bold=True)
        + _span(f" (≥{near.get('minimum', '')})", MUTED, size="small"),
        "<b>Press any button</b> to keep this map.",
        _span(_map_text(near.get("map")), MUTED, size="small"),
        _span(f"Rerolling continues in {remaining} s", MUTED, size="small"),
    ]


def result_card(status: dict, footer: str) -> tuple[str, list[str]]:
    kept = status.get("state") == "kept"
    title = "Map kept" if kept else "Target map found"
    return FOUND, [
        _span(title, bold=True, size="large") + "  " + _span(status.get("target") or "", FOUND, bold=True),
        f"<b>{status.get('rerolls', 0)}</b> rerolls · <b>{format_duration(status.get('elapsed', 0))}</b>"
        + _span(_rate(status), MUTED, size="small"),
        _span(_map_text(status.get("found")), MUTED, size="small"),
        _span(footer, MUTED, size="small"),
    ]


class OverlayController:
    def __init__(self, *, user: str, log: Callable[[str], None],
                 open_game_clock: Callable[[], Callable[[], tuple[bool, float]] | None]) -> None:
        self._user = user
        self._log = log
        self._open_game_clock = open_game_clock
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._last_write = 0.0
        self._pending: tuple[str, list[str]] | None = None
        self._generation = 0  # bumps on every new hunt; stale result watchers stop
        self._near_deadline = 0.0
        self._closed = threading.Event()
        # Throttled writes leave the newest card pending; this lands it.
        threading.Thread(target=self._flush_loop, name="BonkOverlayFlush", daemon=True).start()

    def _flush_loop(self) -> None:
        while not self._closed.wait(WRITE_INTERVAL):
            self.flush()

    def close(self) -> None:
        self._closed.set()
        self.new_hunt()
        self.hide()

    # -- process ---------------------------------------------------------------
    def _ensure_process(self) -> bool:
        if self._process is not None and self._process.poll() is None:
            return True
        if not os.path.exists(SYSTEM_PYTHON):
            return False
        try:
            account = pwd.getpwnam(self._user)
        except KeyError:
            return False
        env = {
            # A clean environment: Decky's bundled interpreter exports its own
            # library paths, which would break the system Python's GTK.
            "PATH": "/usr/bin:/bin",
            "HOME": account.pw_dir,
            "USER": self._user,
            "DISPLAY": STEAM_DISPLAY,
            "XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}",
            "LANG": "C.UTF-8",
        }
        try:
            self._process = subprocess.Popen(
                [SYSTEM_PYTHON, OVERLAY_SCRIPT, STATE_PATH, str(os.getpid())],
                env=env, user=account.pw_uid, group=account.pw_gid,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        except OSError as exc:
            self._log(f"[!] On-screen card unavailable: {exc}")
            self._process = None
            return False

    def _write(self, payload: dict) -> None:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.chmod(tmp, 0o644)
        os.replace(tmp, STATE_PATH)

    def _show(self, accent: str, lines: list[str], *, force: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_write < WRITE_INTERVAL:
                self._pending = (accent, lines)
                return
            self._pending = None
            self._last_write = now
            try:
                self._write({"visible": True, "accent": accent, "lines": lines})
            except OSError:
                return
            self._ensure_process()

    def hide(self) -> None:
        with self._lock:
            self._pending = None
            process, self._process = self._process, None
            try:
                self._write({"exit": True})
            except OSError:
                pass
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                process.terminate()

    def flush(self) -> None:
        with self._lock:
            pending = self._pending
        if pending is not None:
            self._show(*pending, force=True)

    # -- events from the scanner ---------------------------------------------------
    def new_hunt(self) -> None:
        with self._lock:
            self._generation += 1

    def on_status(self, status: dict) -> None:
        state = status.get("state")
        if state == "near_miss":
            near = status.get("near_miss") or {}
            if self._near_deadline < time.monotonic():
                self._near_deadline = time.monotonic() + float(near.get("seconds", 10))
            remaining = max(0, math.ceil(self._near_deadline - time.monotonic()))
            self._show(*near_miss_card(status, remaining), force=True)
            threading.Thread(target=self._near_miss_ticker, args=(status,), daemon=True).start()
            return
        self._near_deadline = 0.0
        if state in ("scanning", "waiting_unpause"):
            self._show(*hunting_card(status))
        elif state == "starting":
            self._show(ACCENT, [_span("BonkScanner", bold=True, size="large"), escape(status.get("message", ""))])

    def _near_miss_ticker(self, status: dict) -> None:
        # Repaints the countdown once a second while the hold lasts.
        generation = self._generation
        while generation == self._generation and self._near_deadline > time.monotonic():
            time.sleep(1.0)
            if self._near_deadline <= time.monotonic():
                break
            remaining = max(0, math.ceil(self._near_deadline - time.monotonic()))
            self._show(*near_miss_card(status, remaining), force=True)

    def on_finished(self, status: dict) -> None:
        """A hunt ended: keep the result card up as long as it is useful."""
        self._near_deadline = 0.0
        if status.get("state") not in ("found", "kept", "already_matches"):
            self.hide()
            return
        generation = self._generation
        threading.Thread(target=self._linger, args=(status, generation), daemon=True).start()

    def _linger(self, status: dict, generation: int) -> None:
        present = status.get("state") == "kept"
        read_clock = None
        if not present:
            try:
                read_clock = self._open_game_clock()
            except Exception:
                read_clock = None
        seen_stopped, resumed_at, last_timer = False, (time.monotonic() if present else None), None
        started = time.monotonic()
        while generation == self._generation:
            now = time.monotonic()
            if resumed_at is None:
                running = None
                if read_clock is not None:
                    try:
                        paused, timer = read_clock()
                        running = (not paused) and last_timer is not None and timer - last_timer > 0.02
                        stopped = paused or (last_timer is not None and timer - last_timer <= 0.02)
                        last_timer = timer
                        if stopped:
                            seen_stopped = True
                    except Exception:
                        read_clock = None
                if (seen_stopped and running) or (read_clock is None and now - started > 60):
                    resumed_at = now
                footer = "Waiting for you - unpause to continue."
            else:
                remaining = LINGER_SECONDS - (now - resumed_at)
                if remaining <= 0:
                    break
                footer = f"Closing in {math.ceil(remaining)} s"
            self._show(*result_card(status, footer), force=True)
            time.sleep(0.25)
        if generation == self._generation:
            self.hide()
