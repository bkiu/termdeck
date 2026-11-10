#!/usr/bin/env python3
"""
Xbox 360 Chatpad -> Linux keyboard driver for the term/deck (Raspberry Pi 3A+).

The Chatpad is a 19200 8N1 serial device wired straight to the Pi's UART:

  * pad TX  -> Pi GPIO15 (header pin 10, RXD): received by the PL011 UART,
               /dev/serial0.
  * pad RX  <- Pi GPIO14 (header pin 8, TXD): the UART's own transmit line.

If GPIO14 is unusable on your board, set "tx_gpio" in the config to a free
GPIO and wire the pad's RX there instead. The driver then clocks the two
host messages out by hand on that pin (see README, "Bit-banged transmit").

Key events are injected through a uinput virtual keyboard, so the Chatpad is
an ordinary keyboard everywhere, including the console login prompt.

Protocol (Cliff Biffle's Chatpad notes / Arduino library):

  host -> pad  Init   87 02 8C 1F CC    the pad is silent until it gets this
  host -> pad  Awake  87 02 8C 1B D0    keep-alive, at least every ~4 s
  pad  -> host 8-byte frames, sum of all 8 bytes == 0 (mod 256):
      key report     B4 C5 xx MM K0 K1 xx CS   MM = modifier bits, K0/K1 keys
      status report  A5 .. .. .. .. .. .. CS   ignored, but counts as "alive"
  modifier bits: 0x01 Shift  0x02 Green  0x04 Orange  0x08 People

Everything about the layout lives in the JSON config (see config.json):
"keys" is the base layer (pad keycode -> key), "green_layer"/"orange_layer"
override base keys while that modifier is held. People is fixed: Ctrl on its
own, Alt with Green, Gui (Super) with Orange.

Modes:
  (default)    run as the keyboard driver
  --debug      also print every frame and every injected key
  --raw        no keyboard; print what is sent and every byte received
               (for checking the wiring)
  --selftest   offline logic test; also exercises uinput when run as root
"""

import argparse
import fcntl
import gc
import json
import mmap
import os
import select
import struct
import sys
import time

import serial
from evdev import ecodes


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
INIT_MSG = bytes([0x87, 0x02, 0x8C, 0x1F, 0xCC])
AWAKE_MSG = bytes([0x87, 0x02, 0x8C, 0x1B, 0xD0])

FRAME_LEN = 8
HEADER_KEY = 0xB4          # key report, byte 0
HEADER_KEY2 = 0xC5         # key report, byte 1
HEADER_STATUS = 0xA5       # status report, byte 0

MOD_SHIFT = 0x01
MOD_GREEN = 0x02
MOD_ORANGE = 0x04
MOD_PEOPLE = 0x08

# Keycodes the pad reports for the modifier keys themselves (debug output).
SPECIAL_KEY_NAMES = {0x81: "shift", 0x82: "green", 0x83: "people", 0x84: "orange"}


class FrameParser:
    """Turns the pad's byte stream into validated 8-byte frames.

    Resyncs on a frame header and drops anything whose checksum fails, which
    covers the odd bursts the pad emits right after Init.
    """

    def __init__(self):
        self.buf = bytearray()
        self.dropped = 0           # bytes discarded while resyncing

    def feed(self, data):
        """Append data; return the list of complete valid frames."""
        self.buf += data
        frames = []
        buf = self.buf
        while len(buf) >= FRAME_LEN:
            b0 = buf[0]
            is_key = b0 == HEADER_KEY and buf[1] == HEADER_KEY2
            if (is_key or b0 == HEADER_STATUS) and sum(buf[:FRAME_LEN]) % 256 == 0:
                frames.append(bytes(buf[:FRAME_LEN]))
                del buf[:FRAME_LEN]
            else:
                del buf[0]
                self.dropped += 1
        if len(buf) > 4096:        # pad streaming garbage; start over
            buf.clear()
        return frames

    def reset(self):
        self.buf.clear()


def is_key_frame(frame):
    return frame[0] == HEADER_KEY


def make_key_frame(mods, key0=0, key1=0):
    """Build a valid key report (used by the self-test)."""
    f = bytearray(FRAME_LEN)
    f[0], f[1], f[3], f[4], f[5] = HEADER_KEY, HEADER_KEY2, mods, key0, key1
    f[7] = (-sum(f)) % 256
    return bytes(f)


# ---------------------------------------------------------------------------
# Key names: config targets are a single character or a Linux key name
# ---------------------------------------------------------------------------
_LBRACK = "KEY_LEFTBRACE" if hasattr(ecodes, "KEY_LEFTBRACE") else "KEY_LEFTBRACKET"
_RBRACK = "KEY_RIGHTBRACE" if hasattr(ecodes, "KEY_RIGHTBRACE") else "KEY_RIGHTBRACKET"

UNSHIFTED = {
    **{c: "KEY_" + c.upper() for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
    " ": "KEY_SPACE", ",": "KEY_COMMA", ".": "KEY_DOT", "/": "KEY_SLASH",
    ";": "KEY_SEMICOLON", "'": "KEY_APOSTROPHE", "[": _LBRACK, "]": _RBRACK,
    "\\": "KEY_BACKSLASH", "-": "KEY_MINUS", "=": "KEY_EQUAL", "`": "KEY_GRAVE",
}
SHIFTED = {
    "!": "KEY_1", "@": "KEY_2", "#": "KEY_3", "$": "KEY_4", "%": "KEY_5",
    "^": "KEY_6", "&": "KEY_7", "*": "KEY_8", "(": "KEY_9", ")": "KEY_0",
    "+": "KEY_EQUAL", "_": "KEY_MINUS", "{": _LBRACK, "}": _RBRACK,
    "|": "KEY_BACKSLASH", "~": "KEY_GRAVE", '"': "KEY_APOSTROPHE",
    ":": "KEY_SEMICOLON", "<": "KEY_COMMA", ">": "KEY_DOT", "?": "KEY_SLASH",
}
NAME_ALIASES = {
    "ESCAPE": "KEY_ESC", "TAB": "KEY_TAB", "RETURN": "KEY_ENTER", "CR": "KEY_ENTER",
    "LCTRL": "KEY_LEFTCTRL", "RCTRL": "KEY_RIGHTCTRL",
    "LALT": "KEY_LEFTALT", "RALT": "KEY_RIGHTALT",
    "LSHIFT": "KEY_LEFTSHIFT", "RSHIFT": "KEY_RIGHTSHIFT",
    "LGUI": "KEY_LEFTMETA", "RGUI": "KEY_RIGHTMETA", "MENU": "KEY_MENU",
    "UP": "KEY_UP", "DOWN": "KEY_DOWN", "LEFT": "KEY_LEFT", "RIGHT": "KEY_RIGHT",
    "PGUP": "KEY_PAGEUP", "PGDN": "KEY_PAGEDOWN", "HOME": "KEY_HOME",
    "END": "KEY_END", "INSERT": "KEY_INSERT", "DEL": "KEY_DELETE",
    "KEY_LEFTBRACKET": _LBRACK, "KEY_LEFTBRACE": _LBRACK,
    "KEY_RIGHTBRACKET": _RBRACK, "KEY_RIGHTBRACE": _RBRACK,
}

_KEYNAMES = {v: k for k, v in ecodes.ecodes.items()
             if isinstance(v, int) and k.startswith("KEY_")}


def keyname(code):
    return _KEYNAMES.get(code, str(code))


def resolve_target(target, log=print):
    """Config target -> set of Linux keycodes (a shifted char adds LEFTSHIFT)."""
    if not isinstance(target, str) or target == "":
        return set()
    name = NAME_ALIASES.get(target.strip().upper(), target.strip())
    if hasattr(ecodes, name) and name.startswith("KEY_"):
        return {getattr(ecodes, name)}
    if len(target) == 1:
        if target in UNSHIFTED:
            return {getattr(ecodes, UNSHIFTED[target])}
        if target in SHIFTED:
            return {getattr(ecodes, SHIFTED[target]), ecodes.KEY_LEFTSHIFT}
    log("warning: unknown key target %r ignored" % (target,))
    return set()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "port": "/dev/serial0",
    "baud": 19200,
    "tx_gpio": None,             # bit-bang host->pad on this GPIO (None = UART TX)
    "awake_interval": 1.0,       # seconds between keep-alives while online
    "init_interval": 1.0,        # seconds between Init attempts while offline
    "reinit_timeout": 5.0,       # silence this long -> pad considered offline
    "device_name": "Xbox 360 Chatpad",
    "repeat_ms": [250, 50],      # auto-repeat [delay, period]
    "keys": {
        "0x17": "1", "0x16": "2", "0x15": "3", "0x14": "4", "0x13": "5",
        "0x12": "6", "0x11": "7", "0x67": "8", "0x66": "9", "0x65": "0",
        "0x27": "q", "0x26": "w", "0x25": "e", "0x24": "r", "0x23": "t",
        "0x22": "y", "0x21": "u", "0x76": "i", "0x75": "o", "0x64": "p",
        "0x37": "a", "0x36": "s", "0x35": "d", "0x34": "f", "0x33": "g",
        "0x32": "h", "0x31": "j", "0x77": "k", "0x72": "l", "0x62": ",",
        "0x46": "z", "0x45": "x", "0x44": "c", "0x43": "v", "0x42": "b",
        "0x41": "n", "0x52": "m", "0x53": ".",
        "0x63": "KEY_ENTER", "0x54": "KEY_SPACE", "0x71": "KEY_BACKSPACE",
        "0x55": "KEY_LEFT", "0x51": "KEY_RIGHT",
    },
    "green_layer": {
        "1": "!", "2": "@", "3": "#", "4": "$", "5": "%",
        "6": "^", "7": "&", "8": "*", "9": "(", "0": ")",
    },
    "orange_layer": {
        "q": "?", "a": "_", "z": "~", "m": "+", "n": "=",
    },
}


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    elif path:
        print("note: no config at %s, using built-in defaults" % path, flush=True)
    return cfg


# ---------------------------------------------------------------------------
# Host -> pad transmit
# ---------------------------------------------------------------------------
class UartTx:
    """Send through the serial port's own TX line."""

    def __init__(self, ser):
        self.ser = ser

    def write(self, data):
        self.ser.write(data)
        self.ser.flush()

    def close(self):
        pass


class GpioTx:
    """Bit-bang 8N1 on a GPIO through /dev/gpiomem (BCM283x register map).

    Fallback for boards whose UART TX pin (GPIO14) is unusable; the pad's
    RX is wired to a spare GPIO instead and "tx_gpio" names it in the
    config. Only two short messages a second go this way. The line idles high; each
    message is clocked against absolute deadlines so one late edge does not
    shift the rest. If the process may use CAP_SYS_NICE, the burst runs under
    SCHED_FIFO so the scheduler cannot take the CPU away mid-byte.
    """

    GPFSEL0, GPSET0, GPCLR0, GPLEV0 = 0x00, 0x1C, 0x28, 0x34

    def __init__(self, gpio, baud):
        if not 0 <= gpio <= 27:
            raise ValueError("tx_gpio must be a header GPIO (0-27)")
        self.gpio = gpio
        self.bit_ns = round(1e9 / baud)
        fd = os.open("/dev/gpiomem", os.O_RDWR | os.O_SYNC)
        try:
            self.mem = mmap.mmap(fd, 4096)
        finally:
            os.close(fd)
        self.mask = struct.pack("<I", 1 << gpio)
        self._set = slice(self.GPSET0, self.GPSET0 + 4)
        self._clr = slice(self.GPCLR0, self.GPCLR0 + 4)
        self.mem[self._set] = self.mask                 # high before enabling output
        fsel = self.GPFSEL0 + 4 * (gpio // 10)
        shift = 3 * (gpio % 10)
        val = struct.unpack("<I", self.mem[fsel:fsel + 4])[0]
        self.mem[fsel:fsel + 4] = struct.pack("<I", (val & ~(7 << shift)) | (1 << shift))
        self._encoded = {}
        self.realtime = self._probe_realtime()

    def _probe_realtime(self):
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(10))
            os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
            return True
        except (PermissionError, OSError):
            return False

    def _encode(self, data):
        seq = self._encoded.get(data)
        if seq is None:
            seq = []
            for b in data:
                for bit in [0] + [(b >> i) & 1 for i in range(8)] + [1]:
                    seq.append(self._set if bit else self._clr)
            self._encoded[data] = tuple(seq)
        return seq

    def level(self, gpio=None):
        """Current input level of a GPIO (0/1)."""
        g = self.gpio if gpio is None else gpio
        return (struct.unpack("<I", self.mem[self.GPLEV0:self.GPLEV0 + 4])[0] >> g) & 1

    def write(self, data):
        seq = self._encode(bytes(data))
        mem, mask, bit_ns = self.mem, self.mask, self.bit_ns
        now = time.perf_counter_ns
        gc_on = gc.isenabled()
        gc.disable()
        if self.realtime:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(10))
        try:
            t = now()
            for i, reg in enumerate(seq):
                mem[reg] = mask
                deadline = t + (i + 1) * bit_ns
                while now() < deadline:
                    pass
        finally:
            if self.realtime:
                os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
            if gc_on:
                gc.enable()

    def close(self):
        self.mem[self._set] = self.mask                 # leave the line idle-high
        self.mem.close()


class Link:
    """The serial link to the pad: UART receive plus one of the transmitters.

    Shared by the driver and the keymap tools so they all send the same way.
    """

    def __init__(self, port, baud, tx_gpio=None):
        self.port, self.baud, self.tx_gpio = port, int(baud), tx_gpio
        self.ser = None
        self.tx = None
        self.gpio = GpioTx(int(tx_gpio), self.baud) if tx_gpio is not None else None

    @classmethod
    def from_config(cls, cfg):
        return cls(cfg["port"], cfg["baud"], cfg.get("tx_gpio"))

    @property
    def is_open(self):
        return self.ser is not None and self.ser.is_open

    def open(self):
        self.close_serial()
        self.ser = serial.Serial(self.port, self.baud, bytesize=8, parity="N",
                                 stopbits=1, timeout=0)
        self.ser.reset_input_buffer()
        self.tx = self.gpio if self.gpio is not None else UartTx(self.ser)

    def close_serial(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.tx = None

    def close(self):
        self.close_serial()
        if self.gpio is not None:
            self.gpio.close()
            self.gpio = None

    def fileno(self):
        return self.ser.fileno()

    def flush_input(self):
        self.ser.reset_input_buffer()

    def send(self, data):
        self.tx.write(data)

    def send_init(self):
        self.send(INIT_MSG)

    def send_awake(self):
        self.send(AWAKE_MSG)

    def read(self):
        """Whatever has arrived (non-blocking)."""
        n = self.ser.in_waiting
        return self.ser.read(n) if n else b""

    def describe_tx(self):
        if self.gpio is not None:
            return "bit-banged on GPIO%d%s" % (
                self.gpio.gpio, " (SCHED_FIFO)" if self.gpio.realtime else "")
        return "UART TX"


# ---------------------------------------------------------------------------
# uinput virtual keyboard (plain ioctls; no helper library needed)
# ---------------------------------------------------------------------------
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503          # _IOW('U', 3, struct uinput_setup[92])
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565

EV_SYN, EV_KEY, EV_REP = 0x00, 0x01, 0x14
REP_DELAY, REP_PERIOD = 0, 1

MODIFIER_KEYS = {ecodes.KEY_LEFTSHIFT, ecodes.KEY_LEFTCTRL,
                 ecodes.KEY_LEFTALT, ecodes.KEY_LEFTMETA}


class Keyboard:
    def __init__(self, name, keycodes, repeat_ms=(250, 50), devnode="/dev/uinput"):
        self.fd = os.open(devnode, os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REP)
        for k in sorted(keycodes):
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, k)
        setup = struct.pack("<HHHH80sI", 0x03, 0x045E, 0x028E, 0x0110,
                            name.encode()[:79], 0)
        fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        time.sleep(0.1)                                 # let udev notice it
        delay, period = repeat_ms
        self._emit(EV_REP, REP_DELAY, int(delay))
        self._emit(EV_REP, REP_PERIOD, int(period))
        self._emit(EV_SYN, 0, 0)
        self.down = set()

    def _emit(self, etype, code, value):
        os.write(self.fd, struct.pack("llHHi", 0, 0, etype, code, value))

    def set_keys(self, desired):
        """Make exactly `desired` the set of held keys.

        A terminal reads the modifier state at the instant a key goes down,
        so modifiers are pressed first and released last.
        """
        release = self.down - desired
        press = desired - self.down
        if not release and not press:
            return [], []
        order = (sorted(release - MODIFIER_KEYS), sorted(release & MODIFIER_KEYS),
                 sorted(press & MODIFIER_KEYS), sorted(press - MODIFIER_KEYS))
        for group, value in zip(order, (0, 0, 1, 1)):
            for k in group:
                self._emit(EV_KEY, k, value)
        self._emit(EV_SYN, 0, 0)
        self.down = set(desired)
        return sorted(press), sorted(release)

    def release_all(self):
        return self.set_keys(set())

    def sysname(self):
        """Kernel name of the created device (e.g. 'input7'), or ''."""
        try:
            buf = bytearray(64)
            fcntl.ioctl(self.fd, 0x8040552C, buf)      # UI_GET_SYSNAME(64)
            return buf.split(b"\0", 1)[0].decode()
        except OSError:
            return ""

    def close(self):
        try:
            self.release_all()
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)


# ---------------------------------------------------------------------------
# Layout: (pad keys, modifier bits) -> Linux keys that should be held
# ---------------------------------------------------------------------------
class Layout:
    def __init__(self, cfg, log=print):
        self.log = log
        self.keys = {int(k, 16): v for k, v in cfg["keys"].items()}
        self.green = dict(cfg.get("green_layer", {}))
        self.orange = dict(cfg.get("orange_layer", {}))

    def all_keycodes(self):
        codes = set(MODIFIER_KEYS)
        for table in (self.keys, self.green, self.orange):
            for target in table.values():
                codes |= resolve_target(target, self.log)
        return codes

    def describe(self, code):
        if code in self.keys:
            return "%02x(%s)" % (code, self.keys[code])
        if code in SPECIAL_KEY_NAMES:
            return "%02x(%s)" % (code, SPECIAL_KEY_NAMES[code])
        return "%02x" % code

    def resolve(self, padkeys, mods):
        desired = set()
        people = bool(mods & MOD_PEOPLE)
        for code in padkeys:
            base = self.keys.get(code)
            if base is None:
                continue
            # With People held, Green/Orange act as Alt/Gui instead of
            # selecting their symbol layer, so the key keeps its base value.
            if mods & MOD_GREEN and not people and base in self.green:
                target = self.green[base]
            elif mods & MOD_ORANGE and not people and base in self.orange:
                target = self.orange[base]
            else:
                target = base
            desired |= resolve_target(target, self.log)
        if mods & MOD_SHIFT:
            desired.add(ecodes.KEY_LEFTSHIFT)
        if people:
            if mods & MOD_GREEN:
                desired.add(ecodes.KEY_LEFTALT)
            elif mods & MOD_ORANGE:
                desired.add(ecodes.KEY_LEFTMETA)
            else:
                desired.add(ecodes.KEY_LEFTCTRL)
        return desired


def frame_keys(frame):
    """The up-to-two pad keycodes held in a key report."""
    return {k for k in (frame[4], frame[5]) if k}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
class Driver:
    def __init__(self, cfg, debug=False, raw=False):
        self.cfg = cfg
        self.debug = debug
        self.raw = raw
        self.awake_interval = float(cfg.get("awake_interval", 1.0))
        self.init_interval = float(cfg.get("init_interval", 1.0))
        self.reinit_timeout = float(cfg.get("reinit_timeout", 5.0))

        self.link = Link.from_config(cfg)
        self.parser = FrameParser()
        self.layout = Layout(cfg, self.log)
        self.kbd = None
        if not raw:
            self.kbd = Keyboard(cfg["device_name"], self.layout.all_keycodes(),
                                cfg.get("repeat_ms", (250, 50)))
            self.log("virtual keyboard '%s' is /sys/class/input/%s"
                     % (cfg["device_name"], self.kbd.sysname() or "?"))
        self.online = False
        self.init_attempts = 0
        self.last_tx = 0.0
        self.last_rx = 0.0
        self.last_nag = 0.0
        self.last_status = None

    def log(self, msg):
        print(msg, flush=True)

    def dbg(self, msg):
        if self.debug or self.raw:
            print(msg, flush=True)

    # ---- link ----------------------------------------------------------
    def open_link(self):
        try:
            self.link.open()
        except Exception as e:
            self.log("cannot open %s: %s (retrying)" % (self.link.port, e))
            return False
        self.log("listening on %s at %d baud; sending %s"
                 % (self.link.port, self.link.baud, self.link.describe_tx()))
        if self.link.gpio is not None:
            self.dbg("GPIO%d idles %s; GPIO15 (pad TX) reads %s"
                     % (self.link.gpio.gpio,
                        "high" if self.link.gpio.level() else "LOW (not idle!)",
                        "high (pad powered)" if self.link.gpio.level(15)
                        else "LOW (pad unpowered or TX wire open)"))
        self.parser.reset()
        return True

    def send(self, msg):
        self.dbg("tx %s" % msg.hex(" "))
        try:
            self.link.send(msg)
        except Exception as e:
            self.log("send failed: %s" % e)
            self.link.close_serial()
        self.last_tx = time.monotonic()

    # ---- state ---------------------------------------------------------
    def go_offline(self, why):
        if self.online:
            self.log("pad offline (%s); sending Init until it answers" % why)
        self.online = False
        self.init_attempts = 0
        self.last_nag = time.monotonic()
        if self.kbd is not None:
            self.kbd.release_all()

    def go_online(self):
        self.online = True
        self.log("pad online after %d Init attempt%s"
                 % (self.init_attempts, "" if self.init_attempts == 1 else "s"))

    def handle_frame(self, frame):
        now = time.monotonic()
        self.last_rx = now
        if not self.online:
            self.go_online()
        if not is_key_frame(frame):
            # The pad streams these continuously (~13/s); only log changes.
            if frame != self.last_status:
                self.dbg("status %s" % frame.hex(" "))
                self.last_status = frame
            return
        padkeys = frame_keys(frame)
        mods = frame[3]
        if self.debug or self.raw:
            self.dbg("frame %s  mods=%s  keys=%s" % (
                frame.hex(" "),
                "".join(n for bit, n in ((MOD_SHIFT, "S"), (MOD_GREEN, "G"),
                                         (MOD_ORANGE, "O"), (MOD_PEOPLE, "P"))
                        if mods & bit) or "-",
                " ".join(self.layout.describe(k) for k in sorted(padkeys)) or "-"))
        if self.kbd is None:
            return
        press, release = self.kbd.set_keys(self.layout.resolve(padkeys, mods))
        if self.debug and (press or release):
            self.dbg("  keys: +%s -%s" % (" ".join(map(keyname, press)) or "-",
                                          " ".join(map(keyname, release)) or "-"))

    # ---- main loop -----------------------------------------------------
    def run(self):
        self.go_offline("start")
        while True:
            if not self.link.is_open:
                if not self.open_link():
                    time.sleep(1.0)
                    continue
                self.go_offline("port reopened")

            now = time.monotonic()
            if self.online and now - self.last_rx > self.reinit_timeout:
                self.go_offline("silent for %.0fs" % self.reinit_timeout)
            if self.online:
                interval = self.awake_interval
                if now - self.last_tx >= interval:
                    self.send(AWAKE_MSG)
            else:
                interval = self.init_interval
                if now - self.last_tx >= interval:
                    self.send(INIT_MSG)
                    self.init_attempts += 1
                    if now - self.last_nag >= 60:
                        self.log("still no answer from the pad (%d Init attempts)"
                                 % self.init_attempts)
                        self.last_nag = now

            wait = max(0.0, min(self.last_tx + interval - time.monotonic(), 0.5))
            try:
                readable, _, _ = select.select([self.link.fileno()], [], [], wait)
                data = self.link.read() if readable else b""
            except (OSError, ValueError, serial.SerialException) as e:
                self.log("serial error: %s" % e)
                self.link.close_serial()
                time.sleep(1.0)
                continue
            if data:
                if self.raw:
                    self.dbg("rx %s" % data.hex(" "))
                for frame in self.parser.feed(data):
                    self.handle_frame(frame)

    def close(self):
        if self.kbd is not None:
            self.kbd.close()
        self.link.close()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def selftest(cfg):
    layout = Layout(cfg)
    K = ecodes

    def keys(mods, *pad):
        return layout.resolve(frame_keys(make_key_frame(mods, *pad)), mods)

    # Layout semantics
    assert keys(0, 0x11) == {K.KEY_7}
    assert keys(0, 0x11, 0x27) == {K.KEY_7, K.KEY_Q}
    assert keys(0) == set()
    assert keys(MOD_SHIFT, 0x37) == {K.KEY_A, K.KEY_LEFTSHIFT}
    assert keys(MOD_GREEN, 0x17) == {K.KEY_1, K.KEY_LEFTSHIFT}          # '!'
    assert keys(MOD_PEOPLE, 0x44) == {K.KEY_C, K.KEY_LEFTCTRL}          # Ctrl+C
    assert keys(MOD_PEOPLE | MOD_GREEN, 0x34) == {K.KEY_F, K.KEY_LEFTALT}
    assert keys(MOD_PEOPLE | MOD_ORANGE, 0x37) == {K.KEY_A, K.KEY_LEFTMETA}
    assert keys(MOD_GREEN, 0x99) == set()                               # unknown pad key
    print("layout: ok")

    # Framing: garbage, a status report, a split frame, a bad checksum
    p = FrameParser()
    good = make_key_frame(MOD_SHIFT, 0x37)
    status = bytearray(b"\xa5\x01\x02\x03\x04\x05\x06\x00")
    status[7] = (-sum(status)) % 256
    bad = bytearray(good)
    bad[7] ^= 0xFF
    stream = b"\x00\xb4\x12" + bytes(status) + bytes(bad) + good[:3]
    out = p.feed(stream)
    assert out == [bytes(status)], out
    out = p.feed(good[3:])
    assert out == [good], out
    assert is_key_frame(good) and not is_key_frame(bytes(status))
    print("framing: ok (dropped %d junk bytes)" % p.dropped)

    # Modifier ordering through a fake keyboard
    events = []

    class FakeKbd(Keyboard):
        def __init__(self):
            self.down = set()

        def _emit(self, etype, code, value):
            events.append((etype, code, value))

    fk = FakeKbd()
    fk.set_keys(keys(MOD_GREEN, 0x17))
    fk.set_keys(set())
    presses = [c for t, c, v in events if t == EV_KEY and v == 1]
    releases = [c for t, c, v in events if t == EV_KEY and v == 0]
    assert presses[0] == K.KEY_LEFTSHIFT and releases[-1] == K.KEY_LEFTSHIFT, events
    assert fk.set_keys(set()) == ([], [])                                # no-op
    print("modifier ordering: ok")

    # Real uinput device, if we may open it: inject a key and read it back.
    if os.access("/dev/uinput", os.W_OK):
        from evdev import InputDevice
        kbd = Keyboard("chatpad selftest", layout.all_keycodes(), cfg.get("repeat_ms", (250, 50)))
        try:
            node = [n for n in os.listdir("/sys/class/input/%s" % kbd.sysname())
                    if n.startswith("event")][0]
            dev = InputDevice("/dev/input/" + node)
            assert dev.repeat.delay == int(cfg.get("repeat_ms", (250, 50))[0]), dev.repeat
            kbd.set_keys({K.KEY_A})
            kbd.set_keys(set())
            time.sleep(0.05)
            seen = [(e.code, e.value) for e in dev.read() if e.type == EV_KEY]
            assert seen == [(K.KEY_A, 1), (K.KEY_A, 0)], seen
            dev.close()
        finally:
            kbd.close()
        print("uinput: ok (auto-repeat %s/%s ms)" % tuple(cfg.get("repeat_ms", (250, 50))))
    else:
        print("uinput: skipped (no write access to /dev/uinput)")
    print("self-test passed")


def main():
    ap = argparse.ArgumentParser(description="Xbox 360 Chatpad keyboard driver")
    ap.add_argument("--config", default="/etc/chatpad/config.json")
    ap.add_argument("--debug", action="store_true", help="print frames and injected keys")
    ap.add_argument("--raw", action="store_true",
                    help="no keyboard; print everything sent and received")
    ap.add_argument("--selftest", action="store_true", help="offline logic test and exit")
    args = ap.parse_args()
    cfg = load_config(args.config)

    if args.selftest:
        selftest(cfg)
        return

    drv = Driver(cfg, debug=args.debug, raw=args.raw)
    try:
        drv.run()
    except KeyboardInterrupt:
        pass
    finally:
        drv.close()


if __name__ == "__main__":
    main()
