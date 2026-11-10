# touchscroll — touchscreen input for a bare-console TUI

Turns the display's capacitive touchscreen into mouse input that a
full-screen terminal program on the Linux console can use:

* **two-finger vertical swipe** → scroll wheel events (phone-style: swipe
  down to see older output)
* **single-finger tap** → a left click at the tapped cell

The term/deck has no desktop and no compositor, so there is otherwise no
pointer at all. `wifipick` uses this for tap-to-select; any TUI that turns
on SGR mouse mode (`\e[?1006h`) gets the same events.

## How it works

The kernel console and `gpm` do not scroll a TUI's internal history (gpm
only does text selection, and the VT ignores wheel and PgUp). What a TUI
does understand is its own mouse protocol: in SGR mouse mode it scrolls
when a wheel escape sequence arrives on its stdin.

`touchscroll.py` therefore:

1. reads the touchscreen's evdev node and, while **two or more** fingers
   are down, converts the average vertical finger movement into scroll
   "ticks";
2. in **tty mode** (default) pushes those ticks onto the active console's
   input queue with `TIOCSTI` as SGR wheel sequences (`ESC[<64;x;yM` up,
   `ESC[<65;x;yM` down). The foreground app parses them as real wheel
   events and scrolls itself.
3. turns a quick, low-drift single-finger touch into an SGR left-button
   press and release at the matching terminal cell.

Running as **root** is required: the kernel only allows `TIOCSTI` on a tty
for its owner or `CAP_SYS_ADMIN`. The daemon re-resolves the foreground
console on every event, so switching VTs sends input to the right place.

The touch device is found automatically: a multitouch device whose name
contains `ft5x06` (the Freenove 4.3" panel), else the first multitouch
device. Use `--device /dev/input/eventN` or `--device-match NAME` to pin
it. Screen size comes from the device's reported axis ranges.

### Tuning

| Flag | Default | Meaning |
|------|---------|---------|
| `--pixels-per-tick` | 8 | Finger travel per scroll tick. Lower is faster. |
| `--tap-max-pixels` | 14 | A tap must stay within this many px of where it started. |
| `--tap-max-ms` | 350 | A tap must lift within this long. |
| `--no-click` | off | Disable tap → click. |
| `--mode` | `tty` | `wheel` emits `REL_WHEEL` from a uinput pointer (for Wayland/X); `keys` emits PgUp/PgDn. Clicks work in `tty` and `wheel`. |

A second finger landing mid-gesture cancels the tap, so a scroll never
produces a stray click.

### Inside a terminal multiplexer

tmux and zellij sit between the console and your app. They only pass the
wheel and click through if their own mouse mode is on (`set -g mouse on`
in tmux; `mouse_mode true` in zellij). With mouse mode on, a swipe scrolls
the pane's scrollback when the app isn't in mouse mode, and goes to the
app when it is.

## Testing the touch

`test_touch.py` reads the touch node and dry-runs the daemon's exact
gesture logic, printing fingers, scroll ticks, and tap cells **without**
injecting anything. It only needs read access to the input node, which the
installer gives you via the `input` group.

```sh
python3 test_touch.py --list          # find the touch /dev/input node
python3 test_touch.py                 # watch live (tap = click, swipe = scroll)
python3 test_touch.py --duration 30   # auto-stop after 30 s
python3 test_touch.py --debug         # also dump every raw EV_ABS event
```

## Install

```sh
sudo ./install.sh
```

Installs `python3-evdev` if missing, copies the files to `/opt/touchscroll`,
disables **gpm** if present (its console text-selection is what highlights
the screen when you drag on it), adds you to the `input` group, and enables
`touchscroll.service` as root.

* Log: `journalctl -u touchscroll -f`. Add `--debug` to `ExecStart` in the
  unit and you'll see `scroll up N` and `click (x, y)` lines.
* Not scrolling? The app in front must have mouse mode on. For apps that
  don't, try `--mode keys`.
* Taps ignored? Raise `--tap-max-pixels` or `--tap-max-ms`.
* Change flags: `sudo systemctl edit touchscroll`, override `ExecStart`,
  then `sudo systemctl restart touchscroll`.

## Files

| File | Purpose |
|------|---------|
| `touchscroll.py` | The daemon: touch reader plus scroll and click emitter. |
| `test_touch.py` | Dry-run harness: prints fingers, scrolls, and clicks without injecting. |
| `touchscroll.service` | systemd unit (runs as root; tty mode needs `TIOCSTI` rights). |
| `install.sh` | Installs everything and enables the service. |

## Uninstall

```sh
sudo systemctl disable --now touchscroll
sudo rm -rf /etc/systemd/system/touchscroll.service /opt/touchscroll
```
