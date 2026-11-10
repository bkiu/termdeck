#!/usr/bin/env python3
"""wifipick — nmcli backend for the WiFi picker TUI (wifipick_tui.py).

No UI lives here: this module only talks to NetworkManager via nmcli.
Import it (or run `wifi`, which starts the TUI).

Needs NetworkManager. nmcli is tried directly first (members of the
`netdev` group may manage connections without a password on Debian), then
again through `sudo -n` as a fallback.

The wifi interface is auto-detected from nmcli; set WIFI_IFACE to force one.
"""

import os
import re
import subprocess
import threading
import time

SCAN_CACHE = {"ts": 0.0, "nets": [], "error": None}
CACHE_TTL = 10  # seconds
CONNECT_LOCK = threading.Lock()
_IFACE = None


def run(cmd, timeout=30):
    """Run a command, retrying with sudo -n if it fails with a permission error."""
    out, err = "", ""
    for prefix in ([], ["sudo", "-n"]):
        r = subprocess.run(prefix + cmd, capture_output=True, text=True, timeout=timeout)
        out, err = r.stdout.strip(), r.stderr.strip()
        if r.returncode == 0:
            return out, err, r.returncode
    return out, err, r.returncode


def iface():
    """The wifi interface name: $WIFI_IFACE, else the first wifi device nmcli knows."""
    global _IFACE
    if _IFACE:
        return _IFACE
    env = os.environ.get("WIFI_IFACE")
    if env:
        _IFACE = env
        return _IFACE
    out, _, rc = run(["nmcli", "-t", "-f", "DEVICE,TYPE", "dev"])
    if rc == 0:
        for line in out.splitlines():
            dev, _, typ = line.partition(":")
            if typ == "wifi":
                _IFACE = dev
                return _IFACE
    return "wlan0"


def scan():
    now = time.time()
    if now - SCAN_CACHE["ts"] < CACHE_TTL and not SCAN_CACHE["error"]:
        return SCAN_CACHE
    SCAN_CACHE["ts"] = now
    out, err, rc = run([
        "nmcli", "-t", "-f", "SSID,BSSID,SIGNAL,SECURITY",
        "dev", "wifi", "list", "--rescan", "yes",
    ])
    nets = []
    if rc == 0:
        for line in out.splitlines():
            # nmcli escapes colons in BSSIDs as \: — split on unescaped only
            parts = re.split(r"(?<!\\):", line)
            if len(parts) < 4:
                continue
            ssid, bssid, signal, security = parts[0], parts[1].replace("\\:", ":"), parts[2], parts[3]
            if not ssid:
                continue
            sec = "none" if security in ("", "--", "802-11-open") else "wpa"
            nets.append({
                "ssid": ssid,
                "bssid": bssid,
                "signal": int(signal) if signal.isdigit() else 0,
                "sec": sec,
            })
        # strongest first, then alphabetical
        nets.sort(key=lambda n: (-n["signal"], n["ssid"].lower()))
        SCAN_CACHE["nets"] = nets
        SCAN_CACHE["error"] = None
    else:
        SCAN_CACHE["error"] = "scan failed: " + (err or out)[:300]
    return SCAN_CACHE


def current_connection():
    # NOTE: `nmcli dev wifi` lists APs; pick the ACTIVE=yes line for our connection
    out, _, rc = run(["nmcli", "-g", "ACTIVE,SSID", "dev", "wifi"])
    ssid = ""
    if rc == 0:
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[0] == "yes":
                ssid = parts[1]
                break
    if not ssid:
        return {"ssid": "", "ip": ""}
    ipout, _, _ = run(["nmcli", "-g", "IP4.ADDRESS", "dev", "show", iface()])
    ip = ipout.splitlines()[0] if ipout.splitlines() else ""
    return {"ssid": ssid, "ip": ip}


def saved_networks():
    """SSIDs of saved WiFi connections (connection names are not reliable)."""
    out, _, rc = run(["nmcli", "-t", "-f", "name,type", "con", "show"])
    if rc != 0:
        return []
    ssids = []
    for line in out.splitlines():
        if not line.endswith(":802-11-wireless") or ":" not in line:
            continue
        name = line.split(":", 1)[0]
        s, _, _ = run(["nmcli", "-g", "802-11-wireless.ssid", "con", "show", name])
        if s:
            ssids.append(s.splitlines()[0])
    return ssids


def connect(ssid, password, wpa, saved=False, autoconnect=True):
    if saved:
        _, err, rc = run(["nmcli", "dev", "wifi", "connect", ssid], timeout=60)
        return rc == 0, err[:400]
    # Remove any stale saved connection for this SSID, then add + connect.
    # Open and WPA networks both go through `con add`: `dev wifi connect
    # --password` is not accepted by every nmcli build.
    with CONNECT_LOCK:
        out, _, _ = run(["nmcli", "-t", "-f", "name,uuid", "con", "show"])
        for line in out.splitlines():
            name, _, uuid = line.partition(":")
            if name == ssid and uuid:
                run(["nmcli", "con", "delete", uuid])

        add = [
            "nmcli", "con", "add", "type", "wifi", "con-name", ssid,
            "ifname", iface(), "ssid", ssid,
        ]
        if password:
            # PSK client only. WPA-Enterprise (wpa-eap) needs an identity
            # and certificate setup that the TUI does not collect.
            add += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        else:
            add += ["wifi-sec.key-mgmt", "none"]

        _, err, rc = run(add, timeout=30)
        if rc != 0:
            return False, err[:400]

        if not autoconnect:
            run(["nmcli", "con", "mod", ssid, "connection.autoconnect", "no"])

        _, err, rc = run(["nmcli", "dev", "wifi", "connect", ssid], timeout=60)
        return rc == 0, err[:400]
