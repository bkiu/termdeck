#!/usr/bin/env python3
"""
touchscroll.py - touchscreen taps and swipes for a bare-console TUI.

Reads the display's capacitive touchscreen over evdev (the FT5x06 on the
Freenove 4.3" DSI panel; any multitouch screen works) and, while two or
more fingers are down, converts their average vertical movement into
scroll events. In tty mode (the default) these are SGR mouse-wheel escape
sequences pushed into the foreground console's input, so any full-screen
TUI that has enabled SGR mouse mode scrolls itself; the other modes emit
from a small uinput virtual device instead.

Swipe direction (phone-style / natural):
    swipe down -> scroll up   (see older output / scrollback)
    swipe up   -> scroll down (see newer output)

In addition to two-finger scrolling, a single-finger tap is injected as a
left mouse click (SGR button 0 press+release) at the tapped location, so
an app in SGR mouse mode can take taps as clicks (wifipick uses this to
select a network). Disable with --no-click.

Usage (needs read access to the touch /dev/input/event* node and
write access to /dev/uinput -- see touchscroll.service / install.sh):

    python3 touchscroll.py [--device-match ft5x06]
                           [--device /dev/input/event0]
                           [--mode tty|wheel|keys]
                           [--pixels-per-tick 8] [--no-click]
                           [--tap-max-pixels 14] [--tap-max-ms 350]
                           [--debug]
"""

import argparse
import ctypes
import fcntl
import os
import struct
import sys
import time

from evdev import AbsInfo, InputDevice, list_devices, UInput, ecodes

# A full swipe across the 480 px display height at 8 px/tick is ~60 ticks;
# at the typical 3 lines/tick terminal scroll speed that's a few pages,
# which feels right on a small deck screen.
DEFAULT_PX_PER_TICK = 8
DEVICE_HINT = "ft5x06"
DEFAULT_TAP_MAX_PX = 14
DEFAULT_TAP_MAX_MS = 350


def _has_mt_tracking(caps):
    """True if the capability dict includes ABS_MT_TRACKING_ID.

    evdev versions differ: EV_ABS maps to either a set/list of codes or a
    list/dict of (code, AbsInfo) pairs -- handle both.
    """
    ab = caps.get(ecodes.EV_ABS)
    if not ab:
        return False
    for entry in ab:
        code = entry[0] if isinstance(entry, tuple) else entry
        if code == ecodes.ABS_MT_TRACKING_ID:
            return True
    return False


def find_touch_device(path=None, hint=DEVICE_HINT):
    """Return the touchscreen path.

    An explicit path wins. Otherwise prefer a multitouch device whose name
    contains `hint`, and fall back to the first multitouch device of any
    name so other panels work without flags.
    """
    if path:
        d = InputDevice(path)
        if hint.lower() in d.name.lower() or "touch" in d.name.lower() \
                or _has_mt_tracking(d.capabilities()):
            return d.path
        raise SystemExit("error: %s does not look like a touchscreen" % path)
    fallback = None
    for p in list_devices():
        try:
            d = InputDevice(p)
        except OSError:
            continue
        if not _has_mt_tracking(d.capabilities()):
            continue
        if hint.lower() in d.name.lower():
            return d.path
        if fallback is None:
            fallback = d
    if fallback is not None:
        print("touchscroll: no device matching %r; using %s (%s)"
              % (hint, fallback.path, fallback.name), file=sys.stderr)
        return fallback.path
    raise SystemExit("error: no multitouch device found under /dev/input "
                     "(is the touchscreen connected? try --device)")


class TouchState:
    """Track active multitouch slots; expose the average Y of all fingers."""

    def __init__(self):
        self.slots = {}   # tracking_id -> (x, y) or (x, None) while partial
        self._cur = None

    def feed(self, event):
        """Feed one evdev event (type/value only)."""
        if event.type == ecodes.EV_ABS:
            if event.code == ecodes.ABS_MT_SLOT:
                self._cur = event.value
            elif event.code == ecodes.ABS_MT_TRACKING_ID:
                if event.value < 0:
                    self.slots.pop(self._cur, None)
                else:
                    self.slots[self._cur] = (None, None)
            elif self._cur is not None and self._cur in self.slots:
                x, y = self.slots[self._cur]
                if event.code == ecodes.ABS_MT_POSITION_X:
                    self.slots[self._cur] = (event.value, y)
                elif event.code == ecodes.ABS_MT_POSITION_Y:
                    self.slots[self._cur] = (x, event.value)

    @property
    def count(self):
        return len(self.slots)

    @property
    def single_xy(self):
        """Position of the lone finger when exactly one is down, else None."""
        if len(self.slots) != 1:
            return None
        x, y = next(iter(self.slots.values()))
        if x is None or y is None:
            return None
        return (x, y)

    @property
    def avg_y(self):
        ys = [y for _, y in self.slots.values() if y is not None]
        if not ys:
            return None
        return sum(ys) / len(ys)


class TtyWheel:
    """Inject SGR mouse-wheel sequences into the foreground console's input.

    A bare Linux console has no mouse -> the only way a fullscreen TUI that
    enables SGR mouse mode (?1006h) can receive a wheel is if the escape
    sequence lands in its stdin. We push each byte onto the
    foreground tty's input queue with TIOCSTI. Requires root (or being the
    tty owner) because modern kernels restrict TIOCSTI to the tty owner.
    """

    TIOCLINUX = 0x541C
    TIOCL_GETFGCONSOLE = 12
    TIOCSTI = 0x5412
    TIOCGWINSZ = 0x5413

    def __init__(self, debug=False, width_px=800, height_px=480):
        self.debug = debug
        self.width_px = max(1, width_px)
        self.height_px = max(1, height_px)
        self._fd = None
        self._fg = None

    def _sti(self, fd, seq):
        """Push a string onto the tty input queue byte by byte."""
        for ch in seq:
            try:
                fcntl.ioctl(fd, self.TIOCSTI, ch.encode())
            except OSError:
                if self.debug:
                    print("tty-wheel: TIOCSTI failed (need root?)", flush=True)
                return False
        return True

    def _open_fg(self):
        # Re-resolve the foreground console each time (cheap) so a VT switch
        # doesn't send scroll to the wrong screen. TIOCL_GETFGCONSOLE returns
        # the console number via the ioctl return value, so use ctypes to
        # read it (fcntl.ioctl would hand back the out-buffer instead).
        probe = os.open("/dev/tty1", os.O_RDWR)
        try:
            buf = ctypes.create_string_buffer(4)
            buf[0] = self.TIOCL_GETFGCONSOLE
            fg = ctypes.CDLL(None).ioctl(probe, self.TIOCLINUX, buf)
        finally:
            os.close(probe)
        if fg != self._fg or self._fd is None:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
            # fg is a 0-based console index (0 = tty1).
            self._fd = os.open("/dev/tty%d" % (fg + 1), os.O_RDWR)
            self._fg = fg
        return self._fd

    def _size(self, fd):
        try:
            row, col = struct.unpack("HHHH", fcntl.ioctl(fd, self.TIOCGWINSZ, b"\x00" * 8))[:2]
        except OSError:
            row, col = 24, 80
        return row, col

    def emit(self, ticks):
        """ticks > 0 = finger moved down = scroll up (SGR button 64)."""
        try:
            fd = self._open_fg()
        except OSError as e:
            if self.debug:
                print("tty-wheel: cannot open fg console: %s" % e, flush=True)
            return
        row, col = self._size(fd)
        x = col // 2 + 1   # 1-based SGR coordinates, center of screen
        y = row // 2 + 1
        button = 64 if ticks > 0 else 65
        for _ in range(abs(ticks)):
            self._sti(fd, "\x1b[<%d;%d;%dM" % (button, x, y))

    def click(self, px, py):
        """Inject a left mouse click at a pixel position.

        SGR mouse coordinates are in terminal cells (1-based). Map the pixel
        position proportionally onto the terminal's row/col grid, then emit a
        left-button press (M) followed by a release (m).
        """
        try:
            fd = self._open_fg()
        except OSError as e:
            if self.debug:
                print("tty-wheel: cannot open fg console: %s" % e, flush=True)
            return
        row, col = self._size(fd)
        cx = int(px / self.width_px * col) + 1
        cy = int(py / self.height_px * row) + 1
        cx = max(1, min(col, cx))
        cy = max(1, min(row, cy))
        self._sti(fd, "\x1b[<0;%d;%dM" % (cx, cy))   # left button press
        self._sti(fd, "\x1b[<0;%d;%dm" % (cx, cy))   # left button release


class TapDetector:
    """Recognize a single-finger tap (click) from a stream of finger updates.

    A click is a down->up that stays within max_move_px of the start and
    lasts no longer than max_ms. Any horizontal drift (drag) or a second
    finger (scroll gesture) cancels it.
    """

    def __init__(self, max_move_px=14, max_ms=350):
        self.max_move_px = max_move_px
        self.max_ms = max_ms
        self._active = False
        self._start = None   # (x, y, t)
        self._last = None    # (x, y)
        self._cancelled = False

    def feed(self, xy, now):
        """Update with the current single-finger position (or None)."""
        if xy is None:
            return
        if not self._active:
            self._active = True
            self._cancelled = False
            self._start = (xy[0], xy[1], now)
        self._last = xy

    def cancel(self):
        self._cancelled = True

    def on_up(self, now):
        """Finger lifted: return (x, y) if this was a click, else None."""
        result = None
        if self._active and not self._cancelled:
            sx, sy, t0 = self._start
            dx = self._last[0] - sx
            dy = self._last[1] - sy
            if (dx * dx + dy * dy) ** 0.5 <= self.max_move_px \
                    and (now - t0) * 1000.0 <= self.max_ms:
                result = (int(sx), int(sy))
        self._active = False
        self._start = None
        self._last = None
        self._cancelled = False
        return result


class Scroller:
    """Turn two-finger vertical movement into scroll events (tty/wheel/keys)."""

    def __init__(self, mode="tty", px_per_tick=DEFAULT_PX_PER_TICK, debug=False,
                 width_px=800, height_px=480,
                 enable_click=True, tap_max_px=DEFAULT_TAP_MAX_PX,
                 tap_max_ms=DEFAULT_TAP_MAX_MS):
        self.mode = mode
        self.px = px_per_tick
        self.debug = debug
        self.touch = TouchState()
        self.last_avg = None
        self.tty = None
        self.ui = None
        self.enable_click = enable_click
        self.taps = TapDetector(tap_max_px, tap_max_ms)
        self._scrolled = False   # True while the current gesture has scrolled
        if mode == "tty":
            self.tty = TtyWheel(debug=debug, width_px=width_px, height_px=height_px)
        elif mode == "wheel":
            # A minimal virtual pointer for a compositor (Wayland/X): wheel
            # events the compositor/terminal consume natively. ABS_X/ABS_Y let
            # a compositor track click position on the virtual pointer.
            self.ui = UInput(
                {ecodes.EV_KEY: [ecodes.BTN_LEFT],
                 ecodes.EV_REL: [ecodes.REL_WHEEL, ecodes.REL_HWHEEL],
                 ecodes.EV_ABS: [(ecodes.ABS_X, AbsInfo(0, 0, width_px - 1, 0, 0, 0)),
                                 (ecodes.ABS_Y, AbsInfo(0, 0, height_px - 1, 0, 0, 0))]},
                name="Touch scroll wheel",
                vendor=0x045e, product=0xf001, version=0x0100, bustype=3,
            )
        else:  # keys
            self.ui = UInput(
                {ecodes.EV_KEY: [ecodes.KEY_PAGEUP, ecodes.KEY_PAGEDOWN,
                                 ecodes.BTN_LEFT]},
                name="Touch scroll keys",
                vendor=0x045e, product=0xf002, version=0x0100, bustype=3,
            )

    def on_report(self):
        """Call once per SYN_REPORT."""
        count = self.touch.count
        if count >= 2:
            self._scrolled = True
            self.taps.cancel()   # a second finger means this is a scroll

        avg = self.touch.avg_y
        if count >= 2 and avg is not None:
            if self.last_avg is not None:
                dy = avg - self.last_avg
                ticks = int(dy // self.px)
                if ticks:
                    self.emit(ticks)
            self.last_avg = avg
        else:
            # Finger count changed (or fingers lifted): reset the baseline so
            # the first move after a new gesture doesn't jump.
            self.last_avg = None

        self._handle_click(count)

    def emit(self, ticks):
        """ticks > 0 = finger moved down = scroll up (older content)."""
        if self.debug:
            print("scroll %s %d" % ("up" if ticks > 0 else "down", abs(ticks)),
                  flush=True)
        if self.mode == "tty":
            self.tty.emit(ticks)
            return
        if self.mode == "wheel":
            self.ui.write(ecodes.EV_REL, ecodes.REL_WHEEL, ticks)
            self.ui.syn()
            return
        # keys: one page key per tick (for terminals that map PgUp/PgDn).
        code = ecodes.KEY_PAGEUP if ticks > 0 else ecodes.KEY_PAGEDOWN
        for _ in range(abs(ticks)):
            self.ui.write(ecodes.EV_KEY, code, 1)
            self.ui.syn()
            self.ui.write(ecodes.EV_KEY, code, 0)
            self.ui.syn()

    def _handle_click(self, count):
        """Track a single-finger tap and emit a click when it resolves."""
        if not self.enable_click:
            return
        now = time.monotonic()
        if count == 1:
            self.taps.feed(self.touch.single_xy, now)
        elif count == 0:
            pos = self.taps.on_up(now)
            if pos is not None and not self._scrolled:
                self.emit_click(pos[0], pos[1])
            self._scrolled = False
        # count >= 2 is handled (cancelled) in on_report

    def emit_click(self, px, py):
        """Inject a left mouse click at a pixel position."""
        if self.debug:
            print("click (%d, %d)" % (px, py), flush=True)
        if self.mode == "tty":
            self.tty.click(px, py)
        elif self.ui is not None:
            if self.mode == "wheel":
                # Report position so a compositor can place the click.
                self.ui.write(ecodes.EV_ABS, ecodes.ABS_X, px)
                self.ui.write(ecodes.EV_ABS, ecodes.ABS_Y, py)
            self.ui.write(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
            self.ui.syn()
            self.ui.write(ecodes.EV_KEY, ecodes.BTN_LEFT, 0)
            self.ui.syn()


def display_size(dev):
    """Best-effort (width, height) in pixels from the device's ABS_X/ABS_Y."""
    try:
        caps = dev.capabilities()
    except Exception:
        return 800, 480

    def abmax(code):
        for entry in caps.get(ecodes.EV_ABS) or []:
            c = entry[0] if isinstance(entry, tuple) else entry
            if c == code and isinstance(entry, tuple):
                return entry[1].maximum
        return None

    x = abmax(ecodes.ABS_X)
    y = abmax(ecodes.ABS_Y)
    return (x + 1 if x is not None else 800, y + 1 if y is not None else 480)


def main():
    ap = argparse.ArgumentParser(description="Two-finger touch swipe -> scroll "
                                             "(plus single-finger taps -> click).")
    ap.add_argument("--device", default=None,
                    help="touch /dev/input path (default: auto-detect)")
    ap.add_argument("--device-match", default=DEVICE_HINT,
                    help="substring to match the device name (default: %(default)s)")
    ap.add_argument("--mode", choices=("tty", "wheel", "keys"), default="tty",
                    help="tty=inject SGR wheel/click into the foreground console "
                         "(default, for the bare console); "
                         "wheel=uinput wheel for a compositor; "
                         "keys=emit PgUp/PgDn")
    ap.add_argument("--pixels-per-tick", type=int, default=DEFAULT_PX_PER_TICK,
                    help="finger travel per scroll tick (default: %(default)s)")
    ap.add_argument("--no-click", action="store_true",
                    help="disable single-finger tap -> click injection")
    ap.add_argument("--tap-max-pixels", type=int, default=DEFAULT_TAP_MAX_PX,
                    help="max drift px for a tap to count as a click (default: %(default)s)")
    ap.add_argument("--tap-max-ms", type=int, default=DEFAULT_TAP_MAX_MS,
                    help="max duration ms for a tap to count as a click (default: %(default)s)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    devpath = find_touch_device(args.device, args.device_match)
    dev = InputDevice(devpath)
    if args.mode in ("wheel", "keys") and not os.access("/dev/uinput", os.W_OK):
        print("warning: no write access to /dev/uinput (run as root)",
              file=sys.stderr)

    width_px, height_px = display_size(dev)
    scroller = Scroller(mode=args.mode, px_per_tick=args.pixels_per_tick,
                        debug=args.debug, width_px=width_px, height_px=height_px,
                        enable_click=not args.no_click,
                        tap_max_px=args.tap_max_pixels, tap_max_ms=args.tap_max_ms)
    print("touchscroll: reading %s (%s), mode=%s, %d px/tick, click=%s"
          % (devpath, dev.name, args.mode, args.pixels_per_tick,
             "on" if not args.no_click else "off"),
          flush=True)

    try:
        for event in dev.read_loop():
            if event.type == ecodes.EV_SYN:
                if event.code == ecodes.SYN_REPORT:
                    scroller.on_report()
            else:
                if args.debug and event.type == ecodes.EV_ABS:
                    print("  %s = %d" % (event.code, event.value), flush=True)
                scroller.touch.feed(event)
    except KeyboardInterrupt:
        pass
    finally:
        if getattr(scroller, "ui", None) is not None:
            try:
                scroller.ui.close()
            except Exception:
                pass
        if getattr(scroller, "tty", None) is not None and scroller.tty._fd is not None:
            try:
                os.close(scroller.tty._fd)
            except OSError:
                pass


if __name__ == "__main__":
    main()
