# wifipick

Touch-friendly WiFi picker for the bare console, as a full-screen curses
TUI. No browser, no desktop; it drives NetworkManager through `nmcli`.

```
 WiFi  connected: MyNetwork (192.168.1.23)                  [R] scan
----------------------------------------------------------------
 >MyNetwork                ████  82%  WPA  *current
   Neighbour               ████  82%  WPA  *saved
   open-net                ███░  45%  open
----------------------------------------------------------------
 tap to select, tap again (or Enter/A) to connect, swipe to scroll
```

## Files

| File | Purpose |
|------|---------|
| `wifipick_tui.py` | The TUI. Run it, or run `wifi`. |
| `wifipick.py` | nmcli backend, imported by the TUI. No UI in it. |
| `wifistatus` | One-line WiFi status for a status bar or pane. Installed to `/usr/local/bin`. |
| `install.sh` | Installs to `/opt/wifipick` and puts `wifi` and `wifistatus` on PATH. |

## Install

```sh
sudo ./install.sh
```

Needs NetworkManager (`nmcli`). Raspberry Pi OS Bookworm and later use it
by default. The installer adds you to the `netdev` group so nmcli works
without a password; failing that, wifipick retries each command through
`sudo -n`.

## Touch

Touch input comes from the [touchscroll](../touchscroll) daemon, which
injects single-finger taps as SGR mouse clicks and two-finger swipes as
SGR wheel sequences into the foreground console. The TUI parses those
itself, because ncurses under `TERM=linux` doesn't decode SGR mouse.

* **tap a network** → select
* **tap the selected network again** → connect. Saved networks connect
  straight away, WPA networks open the password screen, open networks
  connect immediately.
* **two-finger swipe** → scroll the list
* networks marked `*saved` have a key stored in NetworkManager

## Keyboard

| Key | Action |
|-----|--------|
| arrows, `h`/`l`, `w`/`s` | move selection |
| Enter or `a` | connect to the selected network |
| `r` | rescan |
| `q` or Esc | quit |

On the password screen: type the password, **Enter** connects,
**Backspace on an empty field** (or `q`/Esc) cancels. If the network has a
saved key, an empty password plus Enter uses it. For a new network an
**auto-connect next time** toggle is shown (on by default; left/right
flips it). Off stores the password but keeps NetworkManager from joining
the network by itself at boot.

## wifistatus

```
wifistatus --once      MyNetwork 57% 192.168.1.23    (or "WiFi --")
wifistatus             redraws a right-aligned line every 10 s, for a pane
```

Reads `iw`, `/proc/net/wireless` and `ip` rather than nmcli, so it is
cheap enough for a status bar to poll every few seconds on a Pi 3A+. The
interface is the first one `iw dev` lists. `WIFI_IFACE` overrides it and
`WIFI_INTERVAL` changes the loop period.

## Notes

* Connecting goes through NetworkManager; expect the "connecting…" banner
  for 10-15 s while it authenticates.
* New passwords are stored as a NetworkManager connection named after the
  SSID, so later connects use the saved-key path. Any older profile with
  the same name is replaced.
* Only WPA-PSK and open networks are supported. WPA-Enterprise needs an
  identity and certificates the TUI does not collect.
* Scans happen at startup, on `r`, and every 15 s.
* The wifi interface is detected from nmcli. Set `WIFI_IFACE` to force one.
