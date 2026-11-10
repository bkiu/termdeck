#!/usr/bin/env python3
"""
test_touch.py - live test harness for the touchscreen + touchscroll.

Reads the real touch device and runs the *same* gesture logic as
touchscroll.py, but instead of injecting anything into the console it just
*prints* what would happen:

  * how many fingers are down and where
  * detected two-finger scroll ticks (up/down)
  * detected single-finger taps/clicks, with the pixel->cell mapping

This is a dry run: it never touches the foreground tty or /dev/uinput, so it
only needs READ access to the touch /dev/input/event* node (no root). Use it
to confirm the screen is wired up and enumerated, sanity-check the pixel->cell
mapping, and tune --pixels-per-tick / the tap thresholds.

Usage:
    python3 test_touch.py [--list]
                          [--device-match ft5x06] [--device /dev/input/event0]
                          [--duration 30] [--pixels-per-tick 8]
                          [--tap-max-pixels 14] [--tap-max-ms 350]
                          [--debug]

Interact: put one finger on the screen (a tap = click), then try a two-finger
vertical swipe (scroll). Press Ctrl-C to stop, or let --duration elapse.
"""

import argparse
import os
import shutil
import time

from evdev import InputDevice, ecodes
from evdev import list_devices as evdev_list_devices

# Reuse the daemon's exact detection classes so the harness tests real logic.
from touchscroll import (Scroller, find_touch_device, display_size,
                         DEVICE_HINT, DEFAULT_PX_PER_TICK,
                         DEFAULT_TAP_MAX_PX, DEFAULT_TAP_MAX_MS)


def print_device_info(dev):
    w, h = display_size(dev)
    print("device : %s" % dev.name)
    print("path   : %s" % dev.path)
    try:
        caps = dev.capabilities()
        for entry in caps.get(ecodes.EV_ABS) or []:
            code = entry[0] if isinstance(entry, tuple) else entry
            name = ecodes.ecodes.get(code, hex(code))
            if isinstance(entry, tuple) and entry[1] is not None:
                print("  abs  : %-20s max=%d" % (name, entry[1].maximum))
    except Exception as e:
        print("  (could not read caps: %s)" % e)
    print("report : %dx%d px (used for click mapping)" % (w, h))


def show_devices():
    print("input devices under /dev/input:")
    for p in evdev_list_devices():
        try:
            d = InputDevice(p)
        except OSError:
            continue
        print("  %-22s %s" % (d.path, d.name))


def fmt_pos(slots):
    parts = []
    for x, y in sorted(slots.values(), key=lambda v: (v[1] is None, v[1])):
        parts.append("(%d,%d)" % (x, y) if x is not None and y is not None
                     else "(?,?)")
    return " ".join(parts)


def main():
    ap = argparse.ArgumentParser(description="Dry-run test for touchscroll "
                                             "(reads touch, prints, no injection).")
    ap.add_argument("--list", action="store_true",
                    help="list all /dev/input devices and exit")
    ap.add_argument("--device", default=None,
                    help="touch /dev/input path (default: auto-detect)")
    ap.add_argument("--device-match", default=DEVICE_HINT,
                    help="substring to match the device name (default: %(default)s)")
    ap.add_argument("--duration", type=float, default=0,
                    help="stop after N seconds (0 = run until Ctrl-C)")
    ap.add_argument("--pixels-per-tick", type=int, default=DEFAULT_PX_PER_TICK)
    ap.add_argument("--tap-max-pixels", type=int, default=DEFAULT_TAP_MAX_PX)
    ap.add_argument("--tap-max-ms", type=int, default=DEFAULT_TAP_MAX_MS)
    ap.add_argument("--debug", action="store_true",
                    help="print every raw EV_ABS event")
    args = ap.parse_args()

    if args.list:
        show_devices()
        return

    devpath = find_touch_device(args.device, args.device_match)
    dev = InputDevice(devpath)
    print_device_info(dev)

    tw, th = shutil.get_terminal_size()
    w, h = display_size(dev)

    # Build the real Scroller (tty mode) but replace its emitters with
    # printers, so nothing is injected -- pure observation.
    scroller = Scroller(mode="tty", px_per_tick=args.pixels_per_tick,
                        width_px=w, height_px=h,
                        tap_max_px=args.tap_max_pixels,
                        tap_max_ms=args.tap_max_ms)

    stats = {"scrolls": 0, "clicks": 0, "ticks": 0}

    def on_scroll(ticks):
        stats["scrolls"] += 1
        stats["ticks"] += abs(ticks)
        print("  >>> SCROLL %s %d tick(s)  [total %d scrolls / %d ticks]"
              % ("up" if ticks > 0 else "down", abs(ticks),
                 stats["scrolls"], stats["ticks"]))

    def on_click(px, py):
        stats["clicks"] += 1
        cx = max(1, min(tw, int(px / w * tw) + 1))
        cy = max(1, min(th, int(py / h * th) + 1))
        print("  >>> CLICK pixel (%d,%d) -> ~cell (%d,%d)  "
              "[terminal %dx%d, total %d clicks]"
              % (px, py, cx, cy, tw, th, stats["clicks"]))

    scroller.tty.emit = on_scroll
    scroller.tty.click = on_click

    print("\nReady. Tap once = click. Two-finger vertical swipe = scroll.")
    print("Ctrl-C to stop.\n")

    start = time.monotonic()
    last_summary = None
    try:
        for ev in dev.read_loop():
            if args.duration and time.monotonic() - start >= args.duration:
                print("\n(duration elapsed)")
                break

            if ev.type == ecodes.EV_SYN and ev.code == ecodes.SYN_REPORT:
                scroller.on_report()
                count = scroller.touch.count
                if count > 0:
                    summary = "fingers=%d  %s" % (count,
                                                  fmt_pos(scroller.touch.slots))
                    if summary != last_summary:
                        print("  " + summary)
                        last_summary = summary
                elif last_summary is not None:
                    print("  (all fingers lifted)")
                    last_summary = None
            else:
                scroller.touch.feed(ev)
                if args.debug and ev.type == ecodes.EV_ABS:
                    name = ecodes.ecodes.get(ev.code, hex(ev.code))
                    print("    raw %-22s = %d" % (name, ev.value))
    except KeyboardInterrupt:
        print("\n(stopped)")
    finally:
        if scroller.tty._fd is not None:
            try:
                os.close(scroller.tty._fd)
            except OSError:
                pass

    print("\nSummary: %d scrolls (%d ticks), %d clicks."
          % (stats["scrolls"], stats["ticks"], stats["clicks"]))


if __name__ == "__main__":
    main()
