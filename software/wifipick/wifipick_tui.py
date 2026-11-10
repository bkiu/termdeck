#!/usr/bin/env python3
"""
wifipick_tui.py - touch-friendly WiFi picker as a full-screen curses TUI.

Runs in the bare console. Touch input comes from the touchscroll daemon,
which injects single-finger taps as SGR mouse clicks and two-finger swipes
as SGR wheel sequences into the foreground console. With TERM=linux,
ncurses does not decode SGR mouse, so this program sniffs the raw
sequences out of the key stream (read_input).

Touch
    tap a network row    select it
    tap the selected row again   start connecting
    two-finger swipe     scroll the list

Keyboard (any keyboard; written for the Xbox 360 Chatpad)
    left / right (or up/down, h/l, w/s)    move selection
    Enter or A                   connect to the selected network
    (password screen) type the password,
                       Enter = connect, Esc or q = back
    R                          rescan
    Q                          quit

Networking goes through nmcli; see wifipick.py (backend is imported from it).

Usage:
    python3 wifipick_tui.py
"""

import curses
import os
import re
import select
import sys
import threading
import time

# SGR mouse events (injected by touchscroll: tap = click, swipe = wheel).
# With TERM=linux (bare console) ncurses does NOT decode SGR mouse; it passes
# the sequence through as individual keys. We sniff it back out here.
SGR_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])\Z")


def read_input(stdscr):
    """getch(), plus manual SGR mouse parsing.

    Returns (KEY_MOUSE, (bstate, x, y)) for raw SGR events, (KEY_MOUSE, None)
    if ncurses decoded it natively, or (ch, None) for plain keys.
    """
    ch = stdscr.getch()
    if ch != 27:
        return ch, None
    seq = bytearray([27])
    t_end = time.time() + 0.06
    while time.time() < t_end:
        r, _, _ = select.select([sys.stdin], [], [], 0.01)
        if not r:
            break
        c = stdscr.getch()
        if c == -1:
            break
        seq.append(c)
        if c in (ord("M"), ord("m")):
            break
    m = SGR_RE.match(bytes(seq).decode("ascii", "ignore"))
    if not m:
        return 27, None  # bare escape (or a partial sequence: dropped)
    btn, x, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    release = m.group(4) == "m"
    if btn == 0:
        bstate = curses.BUTTON1_RELEASED if release else curses.BUTTON1_PRESSED
    elif btn == 64:
        bstate = curses.BUTTON4_PRESSED
    elif btn == 65:
        bstate = curses.BUTTON5_PRESSED
    else:
        return -1, None
    return curses.KEY_MOUSE, (bstate, x - 1, y - 1)  # SGR is 1-based

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wifipick import scan, saved_networks, current_connection, connect  # noqa: E402

REFRESH_SECS = 15


class State:
    def __init__(self):
        self.nets = []
        self.saved = []
        self.conn = {"ssid": "", "ip": ""}
        self.error = None
        self.sel = 0
        self.scroll = 0
        self.mode = "list"          # list | password
        self.password = ""
        self.autoconnect = True
        self.status = ("", "info")  # text, kind: info|ok|err
        self.busy = False
        self._rows_visible = 20

    def row(self, i):
        return self.nets[i] if 0 <= i < len(self.nets) else None


def bars(sig):
    n = 4 if sig >= 70 else 3 if sig >= 45 else 2 if sig >= 20 else 1
    return "\u2588" * n + "\u2591" * (4 - n)


def refresh_data(st, force=False):
    """Populate nets/saved/conn. Preserves selection by SSID."""
    old_ssid = (st.row(st.sel) or {}).get("ssid")
    try:
        c = scan()
        nets = c["nets"]
        conn = current_connection()
        saved = saved_networks()
    except Exception as e:
        st.status = ("scan failed: %s" % e, "err")
        return
    if nets != st.nets or conn != st.conn or saved != st.saved or force:
        st.nets, st.conn, st.saved = nets, conn, saved
        st.error = c.get("error")
        if old_ssid:
            for i, n in enumerate(nets):
                if n["ssid"] == old_ssid:
                    st.sel = i
                    break
    st.scroll = clamp_scroll(st)


def clamp_scroll(st):
    visible = max(1, st._rows_visible or 10)
    top = max(0, len(st.nets) - visible)
    return max(0, min(st.scroll, top))


def start_connect(st, net, use_saved):
    """Connect in a background thread so the UI stays responsive."""
    ssid = net["ssid"]
    st.busy = True
    st.status = ("connecting to %s ..." % ssid, "info")

    def work():
        try:
            if use_saved:
                ok, err = connect(ssid, "", True, saved=True)
            elif net["sec"] == "none":
                ok, err = connect(ssid, "", False)
            else:
                ok, err = connect(ssid, st.password, True,
                                  autoconnect=st.autoconnect)
        except Exception as e:
            ok, err = False, str(e)
        st.busy = False
        if ok:
            st.status = ("connected to %s" % ssid, "ok")
            st.mode = "list"
            st.password = ""
        else:
            st.status = ("failed: %s" % (err.splitlines()[-1] if err else "?"), "err")
        refresh_data(st)

    threading.Thread(target=work, daemon=True).start()


def draw(stdscr, st):
    stdscr.erase()
    rows, cols = stdscr.getmaxyx()
    st._rows_visible = rows - 4

    try:
        curses.curs_set(0)
    except curses.curs_error:
        pass

    def put(y, x, s, attr=0):
        if y < 0 or y >= rows or x >= cols:
            return
        stdscr.addnstr(y, min(x, cols - 1), s, min(len(s), max(0, cols - 1 - x)), attr)

    # ---- header -------------------------------------------------------
    conn = st.conn
    if conn.get("ssid") and conn["ssid"] != "--":
        hdr = " connected: %s%s" % (conn["ssid"], (" (%s)" % conn["ip"].split("/")[0]) if conn.get("ip") else "")
        put(0, 0, " WiFi " + hdr, curses.A_BOLD | curses.color_pair(2))
    else:
        put(0, 0, " WiFi  (not connected)", curses.A_BOLD | curses.color_pair(3))

    if st.busy:
        put(0, cols - 20, "[connecting...]", curses.color_pair(3))
    else:
        put(0, cols - 12, "[R] scan", curses.A_DIM)
        put(1, cols - 12, "[Q] quit", curses.A_DIM)

    # ---- list ---------------------------------------------------------
    list_top, list_bot = 2, rows - 2
    if not st.nets:
        msg = st.error or "scanning ..."
        put(list_top, 2, msg, curses.color_pair(3) if st.error else 0)
    for r in range(list_top, list_bot):
        i = st.scroll + (r - list_top)
        net = st.row(i)
        if net is None:
            break
        active = (net["ssid"] == conn.get("ssid")) and bool(conn.get("ssid"))
        sel = (i == st.sel)
        tag = " *current" if active else (" *saved" if net["ssid"] in st.saved else "")
        line = " %s%-24s %s %3d%%  %s%s" % (
            ">" if sel else " ", net["ssid"][:24], bars(net["signal"]),
            net["signal"], "open" if net["sec"] == "none" else "WPA", tag)
        if active:
            put(r, 0, line[:cols - 1], curses.A_BOLD | curses.color_pair(2))
        elif sel:
            put(r, 0, line[:cols - 1], curses.A_REVERSE)
        else:
            put(r, 0, line[:cols - 1], 0)
    put(1, 0, "-" * (cols - 1), curses.A_DIM)
    put(rows - 2, 0, "-" * (cols - 1), curses.A_DIM)

    # ---- password mode: centered overlay box ----------------------------
    if st.mode == "password":
        net = st.row(st.sel)
        ssid = (net["ssid"] if net else "?")[:30]
        hint = ("[Backspace] cancel | [Enter] use saved password, or type a new one"
                if ssid in st.saved
                else "[Backspace] cancel | type the password, [Enter] connect")
        auto_on = bool(net and net["sec"] != "none" and ssid not in st.saved)
        lines = ["Password for %s" % ssid, "FIELD", hint]
        if auto_on:
            lines.append("auto-connect next time: %s  [left/right toggle]" %
                         ("on" if st.autoconnect else "off"))
        box_w = min(max(max(len(l) for l in lines if l != "FIELD"),
                        len(st.password) + 4, 44), cols - 2)
        box_h = len(lines) + 2
        top = max(1, (rows - box_h) // 2)
        left = max(1, (cols - box_w) // 2)
        inner = box_w - 2
        # blank the interior so the list doesn't show through
        for r in range(top + 1, top + box_h - 1):
            put(r, left + 1, " " * inner)
        put(top, left, "\u2554" + "\u2550" * inner + "\u2557")
        put(top + box_h - 1, left, "\u255a" + "\u2550" * inner + "\u255d")
        for r in range(top + 1, top + box_h - 1):
            put(r, left, "\u2551")
            put(r, left + box_w - 1, "\u2551")
        put(top + 1, left + 2, lines[0], curses.A_BOLD)
        # full-width inverse field so it reads as a text box
        field = (" %s" % st.password)
        put(top + 2, left + 1, (field + " " * (inner - len(field)))[:inner],
            curses.A_REVERSE)
        put(top + 3, left + 2, lines[2], curses.color_pair(3))
        if auto_on:
            put(top + 4, left + 2, lines[3],
                curses.color_pair(2 if st.autoconnect else 3))

    # ---- status / footer -----------------------------------------------
    text, kind = st.status
    colors = {"ok": 2, "err": 1, "info": 3}
    visible = max(1, st._rows_visible)
    if len(st.nets) > visible:
        count = " %d-%d of %d" % (st.scroll + 1, min(len(st.nets), st.scroll + visible), len(st.nets))
    else:
        count = " %d nets" % len(st.nets)
    if text:
        put(rows - 1, 1, (text + count)[:cols - 3], curses.color_pair(colors[kind]) | curses.A_BOLD)
    elif st.mode == "list":
        put(rows - 1, 0, (" tap to select, tap again (or Enter/A) to connect, swipe to scroll" + count)[:cols - 1],
            curses.A_DIM)
    stdscr.refresh()


def handle_mouse(st, bstate, x, y):
    list_top = 2
    if bstate & curses.BUTTON1_PRESSED:
        if st.mode != "list" or st.busy:
            return
        i = st.scroll + (y - list_top)
        net = st.row(i)
        if net is None:
            return
        if i == st.sel:
            # second tap on the selected row: connect
            use_saved = net["ssid"] in st.saved and net["sec"] != "none"
            if use_saved and not st.password:
                st.password = ""
                start_connect(st, net, True)
            elif net["sec"] == "none":
                start_connect(st, net, False)
            else:
                st.mode = "password"
                st.password = ""
                st.autoconnect = True
        else:
            st.sel = i
            st.scroll = clamp_scroll(st)
    elif bstate & (curses.BUTTON4_PRESSED | curses.BUTTON5_PRESSED):
        if st.mode != "list":
            return
        # touchscroll emits one wheel event per 8 px of swipe
        delta = 1 if bstate & curses.BUTTON4_PRESSED else -1
        st.scroll = max(0, min(st.scroll + delta,
                               max(0, len(st.nets) - max(1, st._rows_visible or 10))))


def run(stdscr):
    st = State()
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    stdscr.keypad(True)
    stdscr.nodelay(True)
    refresh_data(st)

    last_auto = time.time()
    while True:
        r, _, _ = select.select([sys.stdin], [], [], 0.1)
        if r:
            ch, mouse = read_input(stdscr)
        else:
            ch, mouse = -1, None
        now = time.time()

        if now - last_auto > REFRESH_SECS and not st.busy:
            refresh_data(st)
            last_auto = now
            st.status = ("rescanned", "info") if st.status[1] != "info" else st.status

        if ch == curses.KEY_MOUSE:
            if mouse is not None:
                bstate, x, y = mouse
            else:  # ncurses decoded it natively (non-console terminal)
                _, x, y, _, bstate = curses.getmouse()
            handle_mouse(st, bstate, x, y)
        elif st.mode == "list":
            if ch in (curses.KEY_UP, curses.KEY_LEFT, ord("w"), ord("h")):
                st.sel = max(0, st.sel - 1)
                if st.sel < st.scroll:
                    st.scroll = st.sel
            elif ch in (curses.KEY_DOWN, curses.KEY_RIGHT, ord("s"), ord("l")):
                st.sel = min(len(st.nets) - 1, st.sel + 1) if st.nets else 0
                if st.sel >= st.scroll + max(1, st._rows_visible or 10):
                    st.scroll = st.sel - (st._rows_visible or 10) + 1
            elif ch in (curses.KEY_ENTER, 10, ord("a"), ord("A")):
                net = st.row(st.sel)
                if net and not st.busy:
                    use_saved = net["ssid"] in st.saved and net["sec"] != "none"
                    if use_saved:
                        start_connect(st, net, True)
                    elif net["sec"] == "none":
                        start_connect(st, net, False)
                    else:
                        st.mode = "password"
                        st.password = ""
                        st.autoconnect = True
                        try:
                            curses.curs_set(1)
                        except curses.curs_error:
                            pass
            elif ch in (ord("r"), ord("R")):
                st.status = ("scanning ...", "info")
                refresh_data(st, force=True)
                last_auto = now
            elif ch in (ord("q"), ord("Q"), 27):
                try:
                    curses.curs_set(0)
                except curses.curs_error:
                    pass
                return
            elif ch in (curses.KEY_NPAGE,):
                st.scroll = min(st.scroll + 10, max(0, len(st.nets) - (st._rows_visible or 10)))
            elif ch in (curses.KEY_PPAGE,):
                st.scroll = max(st.scroll - 10, 0)
        else:  # password mode
            # 'q' cancels only when the field is empty (it's a valid password char)
            if ch == 27 or (ch in (ord("q"), ord("Q")) and not st.password):
                st.mode = "list"
                st.password = ""
                try:
                    curses.curs_set(0)
                except curses.curs_error:
                    pass
            elif ch in (curses.KEY_BACKSPACE, 127, 8) and not st.password:
                st.mode = "list"
                try:
                    curses.curs_set(0)
                except curses.curs_error:
                    pass
            elif ch in (curses.KEY_ENTER, 10):
                net = st.row(st.sel)
                if net and not st.busy:
                    if not st.password and net["ssid"] in st.saved:
                        start_connect(st, net, True)
                    elif net["sec"] == "none":
                        start_connect(st, net, False)
                    else:
                        start_connect(st, net, False)
                try:
                    curses.curs_set(0)
                except curses.curs_error:
                    pass
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                st.password = st.password[:-1]
            elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT,
                        curses.KEY_UP, curses.KEY_DOWN):
                st.autoconnect = not st.autoconnect
            elif ch >= 32 and ch < 127:
                st.password += chr(ch)

        draw(stdscr, st)  # ~100ms cadence from the select() timeout


def main():
    if not os.isatty(0):
        raise SystemExit("wifipick_tui: run this in a real terminal/console")
    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
