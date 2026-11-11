## The Docs from the Clankers

### Wiring the Chatpad

The Chatpad is wired straight to the Pi's UART on the 40-pin header. There is no USB bridge, microcontroller, or level shifter in between: the Chatpad already runs at 3.3V, so it only needs power, ground, and two data lines. Four wires total.

The two data lines **cross over** (Chatpad TX → Pi RXD, Chatpad RX → Pi TXD), and each data line gets a **1kΩ series resistor** for protection.

#### Overview

```
  Xbox 360 Chatpad                                   Raspberry Pi 3A+
  7-pin JST SH (1.0mm)                               40-pin GPIO header
┌───────────────────────┐                          ┌───────────────────────┐
│ pin 1   +3.3V    red  ├──────────────────────────┤ pin 1    3.3V         │
│ pin 4   GND   orange  ├──────────────────────────┤ pin 6    GND          │
│ pin 3   TX     black  ├────┤ 1kΩ ├───────────────┤ pin 10   GPIO15 / RXD │
│ pin 2   RX     brown  ├────┤ 1kΩ ├───────────────┤ pin 8    GPIO14 / TXD │
│ pin 5–7 audio         │     not connected        │                       │
└───────────────────────┘                          └───────────────────────┘
```

| Chatpad pin | Wire color (verify) | Signal | Via | Pi header pin | Pi signal |
| --- | --- | --- | --- | --- | --- |
| 1 | Red | +3.3V | direct | 1 | 3.3V |
| 4 | Orange | Ground | direct | 6 | GND |
| 3 | Black | Chatpad TX (out) | 1kΩ | 10 | GPIO15 / RXD |
| 2 | Brown | Chatpad RX (in) | 1kΩ | 8 | GPIO14 / TXD |
| 5–7 | Yellow / blue | Audio, unused | — | — | not connected |

Wire colors come from published teardown notes and can vary between units. The pin numbers are what matter, so check them against the connector rather than trusting the colors.

#### The crossover

"TX" on one side always goes to "RX" on the other. The resistors sit on the Chatpad side of the crossover, one per data line.

```
  Chatpad pin 3  TX ────┤ 1kΩ ├────┐     ┌──── Pi pin 8   GPIO14 / TXD
                                    ╲   ╱
                                     ╲ ╱
                                      ╳
                                     ╱ ╲
                                    ╱   ╲
  Chatpad pin 2  RX ────┤ 1kΩ ├────┘     └──── Pi pin 10  GPIO15 / RXD
```

#### Where it lands on the Pi header

Looking down at the Pi with the header in the top corner, pin 1 is the square pad nearest the SD card slot. Only the first ten pins are involved.

```
                       inner row   outer row
                       (odd)       (even)
                       ┌─────┬─────┐
   Chatpad 3.3V ◄──────┤  1  │  2  ├──────► 5V   ✗ do not use
   I2C SDA (PiSugar)   │  3  │  4  │        5V   ✗ do not use
   I2C SCL (PiSugar)   │  5  │  6  ├──────► GND  ◄── Chatpad GND
                       │  7  │  8  ├──────► GPIO14 / TXD ──► Chatpad RX (via 1kΩ)
                       │  9  │ 10  ├──────► GPIO15 / RXD ◄── Chatpad TX (via 1kΩ)
                       │ 11  │ 12  │
                       │  ⋮  │  ⋮  │
                       │ 39  │ 40  │
                       └─────┴─────┘
```

Two things to be careful about on this corner of the header:

- **Pins 2 and 4 are 5V** and sit right next to pin 1. The Chatpad is a 3.3V part, so landing the red wire one pin over will damage it.
- **Pins 3 and 5 are the I2C bus** used by the PiSugar battery board. Leave them alone.

#### Freeing up the serial port

By default the Pi 3A+ hands its good UART (the PL011, `ttyAMA0`) to Bluetooth and runs a login console on the serial port. Both have to be moved off GPIO14/15 before the Chatpad can use them.

1. Run `sudo raspi-config` → Interface Options → Serial Port. Answer **No** to a login shell over serial and **Yes** to enabling the serial hardware.
2. Add the following to `/boot/firmware/config.txt`:

```
enable_uart=1
dtoverlay=disable-bt
```

3. Reboot. `/dev/serial0` should now point at `ttyAMA0`, and that is the device the Chatpad driver opens at 19,200 baud, 8N1.

`dtoverlay=disable-bt` gives up Bluetooth entirely. If Bluetooth is wanted later, `dtoverlay=miniuart-bt` keeps it, but then the core clock has to stay fixed so the baud rate is accurate.



### Software [AI-generated]

The deck runs Raspberry Pi OS Lite (Debian 13 "trixie", 64-bit) with no
desktop. Everything happens on the Linux console inside a terminal
multiplexer, so each piece of hardware that the stock OS doesn't know how
to drive gets a small daemon or TUI in the `software` directory. All of it
is plain Python 3 against Debian packages (`python3-serial`,
`python3-evdev`, NetworkManager), no pip, no compilers, and each directory
has its own `install.sh` and README.

| Directory | What it does | Runs as |
|-----------|--------------|---------|
| [`software/chatpad`](software/chatpad) | Xbox 360 Chatpad → Linux keyboard | systemd service, before login |
| [`software/touchscroll`](software/touchscroll) | touchscreen taps and swipes → mouse input for console TUIs | systemd service |
| [`software/wifipick`](software/wifipick) | WiFi picker TUI plus a status-bar one-liner | `wifi` command |

Install them in that order: wifipick's touch input depends on touchscroll.

```sh
cd software/chatpad     && sudo ./install.sh   # reboot once for the UART
cd ../touchscroll       && sudo ./install.sh
cd ../wifipick          && sudo ./install.sh
```

#### Chatpad keyboard driver

The Chatpad speaks plain serial at 19,200 baud but stays silent until the
host sends it a five-byte Init message, and goes back to sleep unless it
gets a keep-alive every few seconds. The driver opens `/dev/serial0`,
sends those messages on a timer, reassembles the pad's 8-byte key reports
(resyncing on the header bytes and checking the checksum, since the pad
emits some junk right after Init), and turns the held-key set into events
on a `uinput` virtual keyboard. Because it is a real kernel input device
it works at the console login prompt, under the multiplexer, and anywhere
else a USB keyboard would. The kernel handles auto-repeat.

The layout lives in `/etc/chatpad/config.json`. The Green and Orange keys
are symbol layers (the number-row symbols on Green, `hjkl` arrows on
Green, brackets, Tab and Esc on Orange), and the People key is a held
modifier: Ctrl on its own, Alt with Green, Super with Orange, so Ctrl+C
and Alt shortcuts work from the pad. `chatpad-map` is a full-screen editor
for the layout with live capture, so you press a physical key and edit
what it produces.

The driver detects the pad going away (no valid frame for five seconds),
releases any held keys, and goes back to sending Init until it answers, so
unplugging and replugging the pad needs no restart. If GPIO14 on your Pi
is damaged, the pad's RX wire can be moved to any spare GPIO and the
driver will bit-bang the two host messages there instead; see the chatpad
README.

#### Touchscreen (touchscroll)

Without a desktop there is no pointer, and the kernel console does nothing
useful with a touchscreen beyond what `gpm` offers, which is text
selection. touchscroll reads the panel's multitouch events directly and
converts them into the one thing a terminal program does understand: its
own mouse protocol. A two-finger vertical swipe becomes mouse-wheel escape
sequences, and a single-finger tap becomes a left click at the matching
terminal cell, both pushed into the foreground console's input queue with
`TIOCSTI`. Any TUI that has turned on SGR mouse mode scrolls and takes
clicks as if a mouse were attached, including inside tmux or zellij when
their mouse mode is on. It needs root for `TIOCSTI` and runs as a systemd
service. A dry-run harness prints what it would inject so you can tune
sensitivity and tap thresholds before relying on it.

#### WiFi picker (wifipick)

A curses TUI over `nmcli` for joining networks from the console: scan,
pick a network by tapping it or with the arrow keys, type the password on
the Chatpad, and NetworkManager stores the profile for next time. Saved
networks reconnect with one tap, open networks join immediately, and a
toggle on the password screen controls whether NetworkManager auto-joins
at boot. The same directory ships `wifistatus`, a shell one-liner that
prints the SSID, signal and IP from `iw` and `/proc/net/wireless` without
touching D-Bus, cheap enough for a status bar to poll every few seconds on
the 3A+.
