#!/usr/bin/env python3
"""
chatpad_remap.py - interactive key remapper for the Xbox 360 Chatpad.

Connects to the Chatpad on the Pi UART, lets you press a key to discover its
code, and assigns it a new value (a character, or a Linux KEY name). It edits
the same /etc/chatpad/config.json the driver uses, so no code changes are
needed.

Usage (needs root: it reads /dev/serial0 and writes /etc/chatpad/config.json):

    sudo python3 /opt/chatpad/chatpad_remap.py
    sudo python3 /opt/chatpad/chatpad_remap.py --config /etc/chatpad/config.json

At the prompt:
    (empty) or Enter   capture the next key you press on the Chatpad
    l                  list the current mapping
    h                  help
    q                  quit (saves)

Capturing a LAYER: hold the layer key on the Chatpad while you press the
target key -- Green for the green symbols, Orange for the orange symbols.
Shift just uppercases and is ignored for mapping purposes. (People is a
modifier key -- Ctrl on its own, Alt with Green, Gui with Orange -- not a
layer, so there is nothing to capture with it.)

Run  sudo systemctl restart chatpad  after saving to apply the new mapping.
"""

import argparse
import json
import os
import select
import sys
import time

import serial

# Reuse the driver's protocol + helpers so there is one source of truth.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chatpad_driver import (  # noqa: E402
    FRAME_LEN, HEADER_KEY, HEADER_KEY2,
    MOD_SHIFT, MOD_GREEN, MOD_ORANGE, MOD_PEOPLE,
    resolve_target, load_config, keyname, Link,
)

BASE = "base"
LAYERS = {"green": "green_layer", "orange": "orange_layer"}

HELP = """\\
Commands (at the 'chatpad>' prompt):
  <Enter> or empty   capture the next key you press on the Chatpad
  l                  list the current mapping
  h                  show this help
  q                  quit and save

Capturing keys:
  Just press a key on the Chatpad (type nothing, press Enter at the prompt).
  For the GREEN / ORANGE symbols, hold that layer key while pressing the key.
  Shift is pass-through and ignored for mapping.
  People is not a layer (it sends Ctrl, or Alt with Green held, or Gui with
  Orange held), so don't hold it while capturing.
  While waiting for a key, press Enter in the terminal to cancel.

New value when remapping (one of):
  <empty>            keep the current value
  del                remove the mapping (base: key does nothing; layer: falls back)
  a single character e.g.  q   &   !   (shifted symbols are handled for you)
  a Linux key name   e.g.  KEY_LEFTCTRL  ESCAPE  TAB  UP  DOWN  KEY_ENTER
"""


class Reader:
    """Minimal Chatpad serial reader (no uinput) for discovering key presses."""

    def __init__(self, port, baud, tx_gpio=None, debug=False):
        self.debug = debug
        # Same link as the driver: UART receive, Init/Awake sent on the UART
        # TX, or bit-banged on "tx_gpio" when the config sets one.
        self.link = Link(port, baud, tx_gpio)
        self.link.open()
        self.buf = bytearray()
        self.last_key = 0
        self._press = None
        self.last_awake = time.monotonic()
        self.link.send_init()

    def _keepalive(self):
        if time.monotonic() - self.last_awake >= 1.0:
            self.link.send_awake()
            self.last_awake = time.monotonic()

    def _on_frame(self, frame):
        mods = frame[3]
        key = frame[4] or frame[5]
        if self.debug:
            print("  [frame] %s mods=0x%02x key=0x%02x"
                  % (frame.hex(" "), mods, key), file=sys.stderr)
        # Detect a press on the up->down transition.
        if key and not self.last_key:
            self._press = (key, mods)
        self.last_key = key

    def _drain(self):
        while len(self.buf) >= FRAME_LEN:
            idx = self.buf.find(HEADER_KEY)
            if idx < 0:
                self.buf.clear(); break
            if idx > 0:
                del self.buf[:idx]
            if self.buf[1] != HEADER_KEY2:
                del self.buf[0]; continue
            frame = bytes(self.buf[:FRAME_LEN])
            if sum(frame) % 256 == 0:
                del self.buf[:FRAME_LEN]
                self._on_frame(frame)
            else:
                del self.buf[0]
        if len(self.buf) > 4096:
            self.buf.clear()

    def wake(self):
        """Reset + wake the pad before a capture (recovers from sleep)."""
        self.link.flush_input()
        self.buf.clear()
        self.last_key = 0
        self._press = None
        self.link.send_init()
        self.last_awake = time.monotonic()

    def capture(self, timeout=30.0):
        """Wait for one key press. Returns (keycode, mods) or None (cancelled)."""
        self.wake()
        start = time.monotonic()
        while True:
            if time.monotonic() - start > timeout:
                print("  (no key detected; timed out)")
                return None
            self._keepalive()
            try:
                r, _, _ = select.select([self.link, sys.stdin], [], [], 0.1)
            except (OSError, ValueError):
                r = []
            if self.link in r:
                try:
                    self.buf += self.link.read()
                except serial.SerialException as e:
                    print("  serial error:", e)
                    return None
                self._drain()
                if self._press:
                    k = self._press
                    self._press = None
                    return k
            if sys.stdin in r:
                sys.stdin.readline()
                print("  (capture cancelled)")
                return None

    def close(self):
        try:
            self.link.close()
        except Exception:
            pass


def describe_mods(mods):
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


def layer_for(mods):
    # People suppresses the symbol layers (it becomes Alt/Gui instead), so
    # it never selects a layer here.
    if mods & MOD_PEOPLE:
        return BASE
    if mods & MOD_GREEN:
        return "green"
    if mods & MOD_ORANGE:
        return "orange"
    return BASE


def list_config(cfg):
    print("\n=== Chatpad mapping ===")
    print("People key: fixed modifier (alone=Ctrl, +Green=Alt, +Orange=Gui)")
    print("\n-- base --")
    keys = cfg.get("keys", {})
    for k in sorted(keys, key=lambda x: int(x, 16)):
        print("  %s -> %s" % (k, keys[k]))
    for lname in ("green", "orange"):
        d = cfg.get(LAYERS[lname], {})
        if d:
            print("\n-- %s layer --" % lname)
            for k, v in d.items():
                print("  %s -> %s" % (k, v))
    print()


def read_value(prompt):
    while True:
        val = input(prompt).strip()
        low = val.lower()
        if low in ("del", "delete", "-"):
            return None  # sentinel: remove
        if val == "":
            return ""     # keep current
        if resolve_target(val):
            return val
        print("  can't resolve %r -- use a character or a KEY name "
              "(e.g. KEY_LEFTCTRL, ESCAPE, UP)." % val)


def current_and_edit(r, cfg, keycode, mods):
    layer = layer_for(mods)
    khex = "0x%02x" % keycode
    base_target = cfg.get("keys", {}).get(khex)

    if layer == BASE:
        if base_target is None:
            print("  key %s is not in the base map yet (new key)." % khex)
            val = read_value("  base map for %s (value, 'del', or empty to skip): " % khex)
            if val == "":
                print("  (skipped)")
                return
            if val is None:
                cfg.get("keys", {}).pop(khex, None)
            else:
                cfg.setdefault("keys", {})[khex] = val
            return
        cur = base_target
        print("  current base: %s" % cur)
        val = read_value("  new value [Enter=keep, del=unset, or value]: ")
        if val == "":
            print("  (unchanged)")
        elif val is None:
            cfg.get("keys", {}).pop(khex, None)
            print("  removed")
        else:
            cfg.setdefault("keys", {})[khex] = val
            print("  -> %s" % val)
        return

    # A layer: entries are keyed by the base target this key produces.
    if base_target is None:
        print("  %s is not in the base map; set its base value first "
              "(capture it with no layer key held)." % khex)
        return
    d = cfg.setdefault(LAYERS[layer], {})
    cur = d.get(base_target)
    cur_s = cur if cur is not None else "(unset - falls back to base %r)" % base_target
    print("  %s layer for base %r: %s" % (layer, base_target, cur_s))
    val = read_value("  new value [Enter=keep, del=unset, or value]: ")
    if val == "":
        print("  (unchanged)")
    elif val is None:
        d.pop(base_target, None)
        print("  removed (falls back to base)")
    else:
        d[base_target] = val
        print("  -> %s" % val)


def main():
    ap = argparse.ArgumentParser(description="Interactive Chatpad key remapper.")
    ap.add_argument("--config", default="/etc/chatpad/config.json")
    ap.add_argument("--port", default=None,
                    help="serial port (default from config / /dev/serial0)")
    ap.add_argument("--debug", action="store_true", help="print raw frames")
    args = ap.parse_args()

    cfg = load_config(args.config)
    port = args.port or cfg.get("port", "/dev/serial0")

    if not os.path.exists(port):
        print("error: %s not found" % port, file=sys.stderr)
        return 1
    if not os.access(args.config, os.W_OK):
        print("warning: %s is not writable (are you root?)" % args.config,
              file=sys.stderr)

    print("Chatpad remapper  (config: %s)" % args.config)
    print("Press <Enter> at the prompt to capture a key, or type a command. "
          "Type 'h' for help.")
    r = Reader(port, cfg.get("baud", 19200), cfg.get("tx_gpio"), debug=args.debug)

    saved = False
    try:
        while True:
            print("\nchatpad> ", end="", flush=True)
            try:
                cmd = sys.stdin.readline().strip()
            except EOFError:
                break
            low = cmd.lower()
            if low in ("q", "quit", "exit"):
                break
            if low == "h" or low == "?":
                print(HELP)
                continue
            if low == "l":
                list_config(cfg)
                continue
            if cmd == "":
                res = r.capture()
                if not res:
                    continue
                keycode, mods = res
                print("  key 0x%02x  (%s)  modifiers: %s"
                      % (keycode, describe_mods(mods),
                         keyname(keycode) if keycode else "-"))
                if not keycode:
                    print("  (a modifier key alone is not remappable)")
                    continue
                current_and_edit(r, cfg, keycode, mods)
                continue
            # Treat any other text as a KEY-name search hint / ignore.
            print("  (unrecognized command %r; type 'h' for help)" % cmd)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        # Save (with a backup) so a Ctrl-C still keeps your changes.
        try:
            # Drop options that no longer exist (People is fixed in code).
            cfg.pop("people_modifier", None)
            cfg.pop("people_layer", None)
            if os.path.exists(args.config):
                backup = args.config + ".bak"
                with open(args.config) as f:
                    bak = f.read()
                new = json.dumps(cfg, indent=2) + "\n"
                if new != bak:
                    with open(backup, "w") as f:
                        f.write(bak)
                    with open(args.config, "w") as f:
                        f.write(new)
                    print("\nSaved config (backup at %s)." % backup)
                    saved = True
                else:
                    print("\nNo changes to save.")
            else:
                with open(args.config, "w") as f:
                    json.dump(cfg, f, indent=2)
                print("\nWrote new config.")
                saved = True
        except OSError as e:
            print("error writing config: %s" % e, file=sys.stderr)
        r.close()
        if saved:
            print("Apply with:  sudo systemctl restart chatpad")
    return 0


if __name__ == "__main__":
    sys.exit(main())
