# Xbox 360 Chatpad → Linux keyboard driver

Makes a wired Xbox 360 Chatpad work as an ordinary Linux keyboard on a
Raspberry Pi. The pad is read on the Pi's UART (`/dev/serial0`, 19200 8N1)
and key events go through a `uinput` virtual keyboard, so the pad works
everywhere, including the console login prompt. It starts at boot and
recovers on its own if the pad is unplugged.

Wiring is in the main [README](../../README.md#wiring-the-chatpad-ai-generated):
four wires, two 1kΩ resistors, no USB bridge.

## Files

| File | Purpose |
|------|---------|
| `chatpad_driver.py` | The driver. Pure Python: pyserial for the UART, plain `ioctl`s for uinput. |
| `config.json` | Layout and link settings. Installed to `/etc/chatpad/config.json`. |
| `chatpad_tui.py` | Full-screen keymap editor (`chatpad-map`) with live key capture. |
| `chatpad_remap.py` | Line-mode remapper, same job, for a plain terminal or over ssh. |
| `chatpad.service` | systemd unit: runs as user `chatpad`, before the login prompt, restarts on failure. |
| `99-chatpad.rules` | udev rule giving the `chatpad` group the UART. |
| `install.sh` | Installs to `/opt/chatpad` and `/etc/chatpad`, configures the UART, starts the service. Idempotent. |

## Install

```sh
sudo ./install.sh        # safe to re-run; keeps an existing /etc/chatpad/config.json
```

The script installs `python3-serial` and `python3-evdev`, adds
`enable_uart=1` and `dtoverlay=disable-bt` to `config.txt`, disables the
serial login getty, and enables the service. A reboot is needed the first
time so `/dev/serial0` comes up on the PL011 UART.

If `cmdline.txt` still has `console=serial0,...` the script warns; fix it
with `raspi-config` → Interface Options → Serial Port (No to login shell,
Yes to serial hardware).

## Protocol

From Cliff Biffle's Chatpad notes, confirmed on real hardware.

* 19200 baud, 8N1, 3.3V.
* Host → pad **Init** `87 02 8C 1F CC`. The pad is silent until it gets this.
* Host → pad **Awake** `87 02 8C 1B D0`, the keep-alive; it must arrive at
  least every ~4 s or the pad sleeps. The driver sends it every 1 s.
* Pad → host: 8-byte frames whose bytes sum to 0 (mod 256).
  * Key report `B4 C5 .. MM K0 K1 .. CS`: `MM` modifier bits, `K0`/`K1` up
    to two held keys (0 = none).
  * Status report `A5 .. .. .. .. .. .. CS`: streamed continuously once the
    pad is awake, ~13 per second. Ignored, but it tells the driver the pad is
    alive.
  * Right after Init the pad sends a few bytes that don't fit the scheme;
    the parser resyncs on the headers and drops anything with a bad checksum.
* Modifier bits: `0x01` Shift, `0x02` Green, `0x04` Orange, `0x08` People.
* Key reports are repeated a few times per press, so the driver tracks the
  held set and only emits changes.

## Layout (`config.json`)

A target is either a single character (the driver picks the Linux key and
adds Left-Shift for shifted symbols) or a Linux key name (`KEY_ENTER`,
`ESCAPE`, `TAB`, `UP`, …).

* `keys` — base layer: pad keycode (hex) → target.
* `green_layer` / `orange_layer` — while Green (square) or Orange (circle)
  is held, these override base keys. Keyed by the base key's own target
  (`"h": "LEFT"` makes Green+H an arrow key).
* **People** is fixed: Ctrl on its own, Alt together with Green, Gui (Super)
  together with Orange. While People is held the symbol layers are off, so
  People+Green+F is Alt+F, not Alt+^.

The shipped layout is tuned for a terminal: the number-row symbols on
Green, `hjkl` arrows on Green, brackets, braces, Tab and Esc on Orange.
The pad's printed Orange legends are not all kept; change whatever you like.

### Changing keys

```sh
sudo systemctl stop chatpad       # the editors open the same UART as the driver
chatpad-map                       # TUI: 1/2/3 switch layer, Enter edits, v = live capture, s saves
sudo python3 /opt/chatpad/chatpad_remap.py   # line-mode version
sudo systemctl restart chatpad    # after saving
```

### Other settings

| Key | Default | Meaning |
|-----|---------|---------|
| `port` | `/dev/serial0` | Serial device to read. |
| `baud` | `19200` | Fixed by the pad. |
| `awake_interval` | `1.0` | Seconds between keep-alives while online. |
| `init_interval` | `1.0` | Seconds between Init attempts while offline. |
| `reinit_timeout` | `5.0` | Silence this long marks the pad offline and releases held keys. |
| `device_name` | `Xbox 360 Chatpad` | Name of the virtual keyboard. |
| `repeat_ms` | `[250, 50]` | Kernel auto-repeat delay and period. |
| `tx_gpio` | unset | Bit-bang transmit on this GPIO instead of the UART TX (below). |

## Running it by hand

```sh
sudo systemctl stop chatpad
sudo python3 /opt/chatpad/chatpad_driver.py --debug     # frames + injected keys
sudo python3 /opt/chatpad/chatpad_driver.py --raw       # wiring check: every byte in and out, no keyboard
sudo python3 /opt/chatpad/chatpad_driver.py --selftest  # offline logic test; as root also round-trips a key through uinput
sudo systemctl start chatpad
```

`--raw` should show `tx 87 02 8c 1f cc` followed within a second by the
pad's junk burst and then the `a5 …` status stream. Silence after several
`tx` lines means the Pi → pad wire (Pi TXD → 1kΩ → pad pin 2) isn't
delivering. A status stream with no `b4 c5` frames when you press keys
means the pad → Pi wire is fine but the pad isn't registering presses.

## How the driver behaves

* **Offline**: sends Init once a second until any valid frame arrives, then
  logs `pad online after N Init attempts`. While it waits it logs a line a
  minute so a dead link shows in `journalctl -u chatpad`.
* **Online**: Awake every 1 s. If nothing valid arrives for 5 s it releases
  every held key, logs `pad offline`, and goes back to sending Init. That is
  what recovers an unplugged and replugged pad.
* Modifiers are pressed before, and released after, the keys they modify,
  because a terminal reads the modifier state at the instant a key goes down.
* Auto-repeat is the kernel's.
* If the serial port goes away the driver keeps retrying to open it.
  systemd restarts it on any crash.

Idle CPU is a `select()` loop woken by the status stream; well under 2 % on
a Pi 3A+.

## Bit-banged transmit (`tx_gpio`)

The PL011 UART can only transmit on GPIO14 (header pin 8). If that pin is
damaged, set `"tx_gpio": <bcm number>` in `config.json`, move the pad's RX
wire (still through its 1kΩ resistor) to that GPIO, and re-run
`install.sh`. The driver then clocks Init and Awake out by hand through
`/dev/gpiomem`, timed against absolute deadlines with the garbage collector
paused and, when allowed, under `SCHED_FIFO` for the ~5 ms a message takes.
The installer adds a systemd drop-in granting the `gpio` group and
`CAP_SYS_NICE`. A garbled message is harmless; the next keep-alive repeats it.

Add `gpio=<n>=op,dh` to `config.txt` so the pin idles high from power-on,
and consider a 1kΩ pull-up to 3.3V on the pad side.

## Uninstall

```sh
sudo systemctl disable --now chatpad
sudo rm -rf /opt/chatpad /etc/chatpad /etc/systemd/system/chatpad.service \
            /etc/systemd/system/chatpad.service.d /etc/udev/rules.d/99-chatpad.rules \
            /etc/tmpfiles.d/chatpad.conf /etc/sudoers.d/chatpad-map /usr/local/bin/chatpad-map
sudo udevadm control --reload-rules
# optionally remove enable_uart=1 / dtoverlay=disable-bt from config.txt
```
