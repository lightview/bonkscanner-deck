#!/usr/bin/env python3
"""On-screen hunt card for Game Mode, drawn over the game by gamescope.

Run by the plugin with the *system* Python (GTK is not in Decky's bundled
interpreter), as the desktop user, on Steam's X display:

    DISPLAY=:0 /usr/bin/python3 deck_overlay.py STATE_JSON PARENT_PID

gamescope composites one window tagged ``GAMESCOPE_EXTERNAL_OVERLAY`` from that
display on top of everything -- the same slot the Steam performance overlay
(mangoapp) uses -- and draws it from the top-left corner of the screen, so the
window covers the whole screen, stays transparent, and paints the card itself.
``GAMESCOPE_NO_FOCUS`` keeps input with the game.

The plugin writes ``STATE_JSON``: ``{"visible": bool, "accent": "#rrggbb",
"lines": [pango markup, ...]}``. This process exits when its parent does, or
when the file says ``{"exit": true}``.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkX11", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GdkX11, GLib, Gtk, Pango, PangoCairo  # noqa: E402,F401

MARGIN = 14
PADDING_X = 16
PADDING_Y = 12
LINE_GAP = 4
MAX_CARD_WIDTH = 560
POLL_MS = 250


def _hex_rgb(value: str) -> tuple[float, float, float]:
    value = (value or "#5B8DEF").lstrip("#")
    try:
        return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (0.36, 0.55, 0.94)


def _tag_as_gamescope_overlay(xid: int) -> None:
    x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XInternAtom.restype = ctypes.c_ulong
    x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x11.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    x11.XFlush.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    dpy = x11.XOpenDisplay(None)
    if not dpy:
        return
    cardinal = x11.XInternAtom(dpy, b"CARDINAL", 0)
    one = ctypes.c_uint32(1)
    for name in (b"GAMESCOPE_EXTERNAL_OVERLAY", b"GAMESCOPE_NO_FOCUS"):
        x11.XChangeProperty(dpy, xid, x11.XInternAtom(dpy, name, 0), cardinal, 32, 0, ctypes.byref(one), 1)
    x11.XFlush(dpy)
    x11.XCloseDisplay(dpy)


class Overlay(Gtk.Window):
    def __init__(self, state_path: str, parent_pid: int) -> None:
        super().__init__(type=Gtk.WindowType.POPUP)
        self.state_path = state_path
        self.parent_pid = parent_pid
        self.state: dict = {"visible": False, "lines": []}
        self._mtime = 0.0

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.set_accept_focus(False)
        self.move(0, 0)
        geometry = screen.get_display().get_monitor(0).get_geometry()
        self.resize(geometry.width, geometry.height)
        self.connect("draw", self._draw)
        self.realize()
        # Tag before mapping, so gamescope sees an overlay from the first frame.
        _tag_as_gamescope_overlay(self.get_window().get_xid())
        GLib.timeout_add(POLL_MS, self._poll)

    # -- state -----------------------------------------------------------------
    def _poll(self) -> bool:
        if not self._parent_alive():
            Gtk.main_quit()
            return False
        try:
            mtime = os.stat(self.state_path).st_mtime
        except OSError:
            return True
        if mtime == self._mtime:
            return True
        self._mtime = mtime
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            return True
        if state.get("exit"):
            Gtk.main_quit()
            return False
        self.state = state
        if state.get("visible") and state.get("lines"):
            if not self.get_visible():
                self.show_all()
            self.queue_draw()
        elif self.get_visible():
            self.hide()
        return True

    def _parent_alive(self) -> bool:
        try:
            os.kill(self.parent_pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # alive, owned by root

    # -- drawing ---------------------------------------------------------------
    def _draw(self, _widget, cr) -> bool:
        cr.set_operator(0)  # CLEAR: everything outside the card stays see-through
        cr.paint()
        cr.set_operator(2)  # OVER
        lines = self.state.get("lines") or []
        if not lines:
            return False

        layouts = []
        font = Pango.FontDescription("Sans 11")
        for markup in lines:
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(font)
            layout.set_width((MAX_CARD_WIDTH - 2 * PADDING_X) * Pango.SCALE)
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
            layout.set_markup(markup, -1)
            layouts.append(layout)
        sizes = [layout.get_pixel_size() for layout in layouts]
        width = min(MAX_CARD_WIDTH, max(w for w, _h in sizes) + 2 * PADDING_X)
        height = sum(h for _w, h in sizes) + LINE_GAP * (len(sizes) - 1) + 2 * PADDING_Y
        screen_width = self.get_allocated_width()
        x, y = screen_width - width - MARGIN, MARGIN

        radius = 12
        cr.new_sub_path()
        cr.arc(x + width - radius, y + radius, radius, -1.5708, 0)
        cr.arc(x + width - radius, y + height - radius, radius, 0, 1.5708)
        cr.arc(x + radius, y + height - radius, radius, 1.5708, 3.1416)
        cr.arc(x + radius, y + radius, radius, 3.1416, 4.7124)
        cr.close_path()
        cr.set_source_rgba(16 / 255, 18 / 255, 26 / 255, 0.88)
        cr.fill_preserve()
        r, g, b = _hex_rgb(self.state.get("accent", ""))
        cr.set_source_rgba(r, g, b, 1.0)
        cr.set_line_width(2)
        cr.stroke()

        cursor = y + PADDING_Y
        for layout, (_w, h) in zip(layouts, sizes):
            cr.move_to(x + PADDING_X, cursor)
            cr.set_source_rgba(0.91, 0.91, 0.94, 1.0)
            PangoCairo.show_layout(cr, layout)
            cursor += h + LINE_GAP
        return False


def main() -> None:
    state_path, parent_pid = sys.argv[1], int(sys.argv[2])
    Overlay(state_path, parent_pid)
    Gtk.main()


if __name__ == "__main__":
    main()
