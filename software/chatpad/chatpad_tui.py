#!/usr/bin/env python3
"""
chatpad_tui.py - full-screen keymap editor for the Xbox 360 Chatpad.

A curses TUI that draws the Chatpad as a little keymap. Switch between the
three layers (Base / Green / Orange), walk the keys, and change what each
produces. It edits the same /etc/chatpad/config.json the driver uses.

There is also a LIVE capture mode: press a key on the real Chatpad and the
matching cell highlights, so you never have to guess keycodes.

Run (root, so it can read /dev/serial0 and write /etc/chatpad/config.json):

    sudo python3 /opt/chatpad/chatpad_tui.py

Controls (browsing)
    arrow keys       move between keys
    1 2 3            Base / Green / Orange layer   (Tab cycles)
    Enter or e       edit the selected key
    c                clear the selected key in the current layer
    v                toggle LIVE capture
    s                save
    ? or h           help
    q                quit (asks to save if there are changes)

Controls (editing, after selecting a key and pressing Enter)
    type a value: a single character (q & ! ...) or a KEY name
                  (KEY_LEFTCTRL, ESCAPE, TAB, UP, DOWN, KEY_ENTER, ...)
    del             clear the mapping
    Backspace       edit the value
    Enter           commit
    Esc             cancel

The bottom-row keys are modifier/layer keys and aren't remappable here:
Shift uppercases, Green/Orange select their symbol layers, and People is
fixed in code (alone=Ctrl, People+Green=Alt, People+Orange=Gui).
"""

import argparse
import json
import os
import select
import sys
import time

import serial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chatpad_driver import (  # noqa: E402
    FRAME_LEN, HEADER_KEY, HEADER_KEY2,
    MOD_SHIFT, MOD_GREEN, MOD_ORANGE, MOD_PEOPLE,
    resolve_target, load_config, Link,
)

# ---------------------------------------------------------------------------
# Keymap layout (logical approximation). Each row = list of chatpad keycodes.
# Every cell also shows its keycode, and LIVE capture selects the right one.
# ---------------------------------------------------------------------------
ROWS = [
    [0x17, 0x16, 0x15, 0x14, 0x13, 0x12, 0x11, 0x67, 0x66, 0x65],  # 1 2 3 4 5 6 7 8 9 0
    [0x27, 0x26, 0x25, 0x24, 0x23, 0x22, 0x21, 0x76, 0x75, 0x64],  # q w e r t y u i o p
    [0x37, 0x36, 0x35, 0x34, 0x33, 0x32, 0x31, 0x77, 0x72, 0x62],  # a s d f g h j k l ,
    [0x46, 0x45, 0x44, 0x43, 0x42, 0x41, 0x52, 0x53, 0x63],        # z x c v b n m . enter
    [0x81, 0x82, 0x84, 0x83, 0x55, 0x54, 0x51, 0x71],             # shift green orange people | left space right backspace
]
KEYCELL = {k: (r, c) for r, row in enumerate(ROWS) for c, k in enumerate(row)}

MOD_KEYS = {0x81, 0x82, 0x83, 0x84}
MOD_LABEL = {0x81: "SHIFT", 0x82: "GREEN", 0x84: "ORANGE", 0x83: "PEOPLE"}

LAYER_NAMES = ["base", "green", "orange"]
LAYER_TAB = ["Base", "Green", "Orange"]
LAYER_KEY = {"base": None, "green": "green_layer", "orange": "orange_layer"}
LAYER_COLORS = [0, 1, 2]  # 0=normal, else color_pair index

SHORT = {
    "KEY_LEFTCTRL": "Ctrl", "KEY_RIGHTCTRL": "RCtrl",
    "KEY_LEFTSHIFT": "LShift", "KEY_RIGHTSHIFT": "RShift",
    "KEY_LEFTALT": "Alt", "KEY_RIGHTALT": "RAlt",
    "KEY_LEFTMETA": "LGui", "KEY_RIGHTMETA": "RGui",
    "KEY_ENTER": "Enter", "KEY_SPACE": "space", "KEY_BACKSPACE": "Bksp",
    "KEY_TAB": "Tab", "KEY_ESC": "Esc",
    "KEY_UP": "Up", "KEY_DOWN": "Dn", "KEY_LEFT": "Lft", "KEY_RIGHT": "Rgt",
}


def shortlabel(v):
    if v is None:
        return "?"
    if len(v) == 1:
        return v
    return SHORT.get(v, v[:6])


def mods2str(mods):
    parts = []
    if mods & MOD_SHIFT:
        parts.append("shift")
    if mods & MOD_GREEN:
        parts.append("green")
    if mods & MOD_ORANGE:
        parts.append("orange")
    if mods & MOD_PEOPLE:
        parts.append("people")
    return ", ".join(parts) if parts else "none"


HELP = [
    "KEYMAP EDITOR",
    "  arrows          move between keys",
    "  1 / 2 / 3       Base / Green / Orange layer   (Tab cycles)",
    "  Enter or e      edit the selected key",
    "  c               clear the selected key in the current layer",
    "  v               toggle LIVE capture (press a real Chatpad key to jump)",
    "  s               save          ? or h  this help         q  quit",
    "",
    "EDIT MODE (select a key, press Enter):",
    "  type a value = a character (q & ! ...) or a KEY name",
    "                 (KEY_LEFTCTRL, ESCAPE, TAB, UP, DOWN, KEY_ENTER, ...)",
    "  type 'del'    = clear the mapping",
    "  Backspace edit     Enter commit      Esc cancel",
    "",
    "LIVE CAPTURE (press v): press a key on the physical Chatpad and its",
    "cell highlights; press Enter to edit it. Colours show a value that is a",
    "layer override; dimmed = no override (falls back to Base).",
]


class SerialReader:
    """Read Chatpad frames; report press transitions (up -> down)."""

    def __init__(self, port, baud, tx_gpio=None):
        self.ok = False
        self.last_key = 0
        self.pending = None
        self.last_awake = time.monotonic()
        self._buf = bytearray()
        self.fd = None
        self.link = None
        try:
            # Same link as the driver: UART receive, Init/Awake sent on the
            # UART TX, or bit-banged on "tx_gpio" when the config sets one.
            self.link = Link(port, baud, tx_gpio)
            self.link.open()
            self.link.send_init()
            self.fd = self.link.fileno()
            self.ok = True
        except Exception as e:
            self.err = str(e)

    def keepalive(self):
        if self.ok and time.monotonic() - self.last_awake >= 1.0:
            try:
                self.link.send_awake()
            except Exception:
                pass
            self.last_awake = time.monotonic()

    def pump(self):
        if not self.ok:
            return
        data = self.link.read()
        if not data:
            return
        self._buf += data
        while len(self._buf) >= FRAME_LEN:
            idx = self._buf.find(HEADER_KEY)
            if idx < 0:
                self._buf.clear(); break
            if idx > 0:
                del self._buf[:idx]
            if self._buf[1] != HEADER_KEY2:
                del self._buf[0]; continue
            frame = bytes(self._buf[:FRAME_LEN])
            if sum(frame) % 256 == 0:
                del self._buf[:FRAME_LEN]
                key = frame[4] or frame[5]
                if key and not self.last_key:
                    self.pending = (key, frame[3])
                self.last_key = key
            else:
                del self._buf[0]
        if len(self._buf) > 4096:
            self._buf.clear()

    def close(self):
        if self.link is not None:
            try:
                self.link.close()
            except Exception:
                pass


def move(sel, dr, dc):
    r, c = sel
    r = max(0, min(len(ROWS) - 1, r + dr))
    c = max(0, min(len(ROWS[r]) - 1, c + dc))
    return (r, c)


def main(stdscr, args):
    import curses
    cfg = load_config(args.config)
    port = args.port or cfg.get("port", "/dev/serial0")

    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.nodelay(True)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)     # green layer
        curses.init_pair(2, curses.COLOR_YELLOW, -1)    # orange layer
        curses.init_pair(4, curses.COLOR_CYAN, -1)      # live flash
        curses.init_pair(5, curses.COLOR_RED, -1)       # error

    reader = SerialReader(port, cfg.get("baud", 19200), cfg.get("tx_gpio"))
    stdin_fd = sys.stdin.fileno()

    sel = (0, 0)
    layer_idx = 0
    state = "browse"
    editbuf = ""
    edit_key = None
    flash_cell = None
    flash_until = 0.0
    live = False
    dirty = False
    saved_any = False
    status = "Ready. Press 'v' for live capture, or navigate + Enter to edit."

    def cur_layer_key():
        return LAYER_KEY[LAYER_NAMES[layer_idx]]

    def layer_dict():
        return cfg.setdefault(cur_layer_key() or "keys", {})

    def current_value(keycode):
        """(display_value, is_override) for the active layer."""
        khex = "0x%02x" % keycode
        if keycode in MOD_KEYS:
            return MOD_LABEL[keycode], True
        base = cfg.get("keys", {}).get(khex)
        if layer_idx == 0:
            return shortlabel(base), (base is not None)
        d = cfg.get(cur_layer_key(), {})
        if base is not None and base in d:
            return shortlabel(d[base]), True
        return shortlabel(base), False

    def commit(value):
        nonlocal dirty, status
        khex = "0x%02x" % edit_key
        if edit_key == 0x83:  # People -> fixed in code, not remappable
            status = ("People is fixed: alone=Ctrl, +Green=Alt, +Orange=Gui "
                      "(not remappable).")
            return
        if edit_key in MOD_KEYS:
            status = "Shift/Green/Orange are layer keys (not remappable)."
            return
        base = cfg.get("keys", {}).get(khex)
        if layer_idx == 0:
            if value is None:
                cfg.get("keys", {}).pop(khex, None)
                status = "Cleared base for %s" % khex
            else:
                cfg.setdefault("keys", {})[khex] = value
                status = "%s -> %s" % (khex, value)
        else:
            if base is None:
                status = "No base value for %s; set its Base value first." % khex
                return
            d = cfg.setdefault(cur_layer_key(), {})
            if value is None:
                d.pop(base, None)
                status = "Cleared %s layer for %r" % (LAYER_NAMES[layer_idx], base)
            else:
                d[base] = value
                status = "%s[%s] -> %s" % (base, LAYER_NAMES[layer_idx], value)
        dirty = True

    def save():
        nonlocal dirty, saved_any
        try:
            # Drop options that no longer exist (People is fixed in code).
            cfg.pop("people_modifier", None)
            cfg.pop("people_layer", None)
            new = json.dumps(cfg, indent=2) + "\n"
            if os.path.exists(args.config):
                with open(args.config) as f:
                    bak = f.read()
                if new != bak:
                    with open(args.config + ".bak", "w") as f:
                        f.write(bak)
                    with open(args.config, "w") as f:
                        f.write(new)
                    saved_any = True
                    return "Saved (backup: %s.bak)" % args.config
                return "No changes to save."
            with open(args.config, "w") as f:
                f.write(new)
            saved_any = True
            return "Wrote new config."
        except OSError as e:
            return "Error saving: %s" % e

    def do_clear(keycode):
        nonlocal dirty, status
        khex = "0x%02x" % keycode
        if keycode in MOD_KEYS:
            status = ("Shift/Green/Orange are layer keys; People is a fixed "
                      "modifier (Ctrl, or Alt/Gui with Green/Orange).")
            return
        if layer_idx == 0:
            if cfg.get("keys", {}).pop(khex, None) is not None:
                dirty = True
                status = "Cleared base for %s" % khex
            else:
                status = "%s already empty" % khex
        else:
            base = cfg.get("keys", {}).get(khex)
            if base is None:
                status = "No base value for %s" % khex
                return
            d = cfg.setdefault(cur_layer_key(), {})
            if base in d:
                d.pop(base)
                dirty = True
                status = "Cleared %s layer for %r" % (LAYER_NAMES[layer_idx], base)
            else:
                status = "No %s-layer value for %r" % (LAYER_NAMES[layer_idx], base)

    def draw():
        stdscr.erase()
        maxy, maxx = stdscr.getmaxyx()
        try:
            stdscr.addstr(0, 0, " Xbox 360 Chatpad - keymap editor ", curses.A_BOLD)
            x = 2
            for i, name in enumerate(LAYER_TAB):
                if i == layer_idx:
                    attr = curses.A_REVERSE | curses.A_BOLD
                elif LAYER_COLORS[i]:
                    attr = curses.color_pair(LAYER_COLORS[i])
                else:
                    attr = 0
                stdscr.addstr(1, x, " %s " % name, attr)
                x += len(name) + 3
            if live:
                stdscr.addstr(1, max(0, maxx - 14), " LIVE CAPTURE ",
                              curses.A_REVERSE | curses.A_BOLD)
            elif not reader.ok:
                stdscr.addstr(1, max(0, maxx - 18), " (serial offline) ", curses.A_BOLD)
        except curses.error:
            pass

        CW, RH = 8, 2
        x0 = max(0, (maxx - len(ROWS[0]) * CW) // 2)
        y0 = 3
        now = time.monotonic()
        for r, row in enumerate(ROWS):
            y = y0 + r * RH
            for c, keycode in enumerate(row):
                x = x0 + c * CW
                if x + CW > maxx:
                    break
                val, ov = current_value(keycode)
                if layer_idx > 0 and LAYER_COLORS[layer_idx]:
                    attr = curses.color_pair(LAYER_COLORS[layer_idx]) if ov else curses.A_DIM
                else:
                    attr = 0
                if (r, c) == sel:
                    attr = curses.A_REVERSE | curses.A_BOLD
                elif flash_cell == (r, c) and now < flash_until:
                    attr = curses.color_pair(4) | curses.A_BOLD
                try:
                    stdscr.addstr(y, x, (val + " ")[:CW - 1], attr)
                    stdscr.addstr(y + 1, x, ("0x%02x" % keycode)[:CW - 1], attr)
                except curses.error:
                    pass

        sy = y0 + len(ROWS) * RH + 1
        r, c = sel
        keycode = ROWS[r][c]
        khex = "0x%02x" % keycode
        base = cfg.get("keys", {}).get(khex)
        if keycode in MOD_KEYS:
            if keycode == 0x83:
                info = ("%s  PEOPLE (fixed: alone=Ctrl, +Green=Alt, "
                        "+Orange=Gui)") % khex
            else:
                info = "%s  %s (modifier / layer key)" % (khex, MOD_LABEL[keycode])
        else:
            val, ov = current_value(keycode)
            info = "%s  base=%r  %s=%s%s" % (
                khex, base, LAYER_NAMES[layer_idx], val,
                "" if ov else " (fallback to base)")
        try:
            stdscr.addstr(sy, 0, (" " + info)[:maxx - 1], curses.A_REVERSE)
            stdscr.addstr(sy + 1, 0, (" " + status)[:maxx - 1],
                          curses.A_BOLD if status.startswith(("Error", "No base")) else 0)
        except curses.error:
            pass

        if state == "edit":
            ey = sy + 3
            prompt = " Set %s [%s]: " % (khex, LAYER_NAMES[layer_idx])
            try:
                stdscr.addstr(ey, 0, (" " + prompt)[:maxx - 1], curses.A_BOLD)
                px = min(len(prompt), maxx - 1)
                stdscr.addstr(ey, px, editbuf[:max(0, maxx - px - 2)], curses.A_UNDERLINE)
                stdscr.addstr(ey, min(px + len(editbuf) + 1, maxx - 1), "_", curses.A_BOLD)
                hint = " type a char or KEY name, 'del'=clear  |  Enter save, Esc cancel"
                stdscr.addstr(ey + 1, 0, (" " + hint)[:maxx - 1], curses.A_DIM)
            except curses.error:
                pass

        stdscr.refresh()

    def show_help():
        stdscr.erase()
        maxy, maxx = stdscr.getmaxyx()
        for i, line in enumerate(HELP[:maxy - 2]):
            try:
                stdscr.addstr(i, 1, line[:maxx - 2])
            except curses.error:
                break
        try:
            stdscr.addstr(maxy - 1, 1, " press any key to continue ", curses.A_REVERSE)
        except curses.error:
            pass
        stdscr.refresh()
        while stdscr.getch() == -1:
            pass

    running = True
    while running:
        draw()
        fds = [stdin_fd]
        if reader.ok and live:
            fds.append(reader.fd)
        try:
            rset, _, _ = select.select(fds, [], [], 0.1)
        except (OSError, ValueError):
            rset = []
        reader.keepalive()

        if reader.ok and live and reader.fd in rset:
            reader.pump()
            if reader.pending:
                k, mods = reader.pending
                reader.pending = None
                if k in KEYCELL:
                    sel = KEYCELL[k]
                    flash_cell = sel
                    flash_until = time.monotonic() + 0.6
                    status = "Captured %s (mods: %s) - Enter to edit" % ("0x%02x" % k, mods2str(mods))
                else:
                    status = "Captured %s (not in layout)" % ("0x%02x" % k)

        ch = stdscr.getch()
        if ch == -1:
            continue

        if state == "edit":
            if ch == 27:  # Esc
                state = "browse"
                status = "Cancelled."
            elif ch in (10, 13, curses.KEY_ENTER):
                text = editbuf.strip()
                if text == "":
                    state = "browse"
                    status = "No change."
                elif text.lower() in ("del", "delete", "-"):
                    commit(None)
                    state = "browse"
                elif resolve_target(text):
                    commit(text)
                    state = "browse"
                else:
                    status = "Unrecognized value %r - try a char or KEY name." % text
            elif ch in (127, 8, curses.KEY_BACKSPACE):
                editbuf = editbuf[:-1]
            elif 32 <= ch <= 126:
                editbuf += chr(ch)
            continue

        # ---- browse ----
        if ch in (curses.KEY_UP,):
            sel = move(sel, -1, 0)
        elif ch in (curses.KEY_DOWN,):
            sel = move(sel, 1, 0)
        elif ch in (curses.KEY_LEFT,):
            sel = move(sel, 0, -1)
        elif ch in (curses.KEY_RIGHT,):
            sel = move(sel, 0, 1)
        elif ch in (ord("1"),):
            layer_idx = 0
        elif ch in (ord("2"),):
            layer_idx = 1
        elif ch in (ord("3"),):
            layer_idx = 2
        elif ch == 9:  # Tab
            layer_idx = (layer_idx + 1) % 3
        elif ch in (curses.KEY_ENTER, 10, 13, ord("e")):
            edit_key = ROWS[sel[0]][sel[1]]
            editbuf = ""
            state = "edit"
            status = "Editing %s [%s]" % ("0x%02x" % edit_key, LAYER_NAMES[layer_idx])
        elif ch in (ord("c"),):
            do_clear(ROWS[sel[0]][sel[1]])
        elif ch in (ord("v"),):
            live = not live
            status = "LIVE capture %s" % ("ON - press a real key" if live else "off")
        elif ch in (ord("s"),):
            status = save()
            dirty = False
        elif ch in (ord("?"), ord("h")):
            show_help()
        elif ch in (ord("q"),):
            if dirty:
                try:
                    stdscr.addstr(0, 0, " Unsaved changes - save? [y]es / [n]o ",
                                  curses.A_REVERSE | curses.A_BOLD)
                    stdscr.refresh()
                except curses.error:
                    pass
                while True:
                    a = stdscr.getch()
                    if a in (ord("y"), ord("Y")):
                        status = save()
                        break
                    if a in (ord("n"), ord("N"), 27):
                        break
                    if a == -1:
                        continue
            running = False

    reader.close()
    if saved_any:
        print("\nChanges saved. Apply with:  sudo systemctl restart chatpad",
              file=sys.stderr)
    if dirty:
        print("WARNING: quit with unsaved changes (not written to %s)." % args.config,
              file=sys.stderr)


def aparse(argv):
    ap = argparse.ArgumentParser(description="Chatpad TUI keymap editor.")
    ap.add_argument("--config", default="/etc/chatpad/config.json")
    ap.add_argument("--port", default=None)
    ap.add_argument("--debug", action="store_true")
    return ap.parse_args(argv)


if __name__ == "__main__":
    import curses
    try:
        curses.wrapper(main, aparse(sys.argv[1:]))
    except KeyboardInterrupt:
        pass
