#!/usr/bin/env bash
#
# install.sh - install the Xbox 360 Chatpad UART keyboard driver.
#
# Tested on Raspberry Pi OS Lite (Debian 12/13). Idempotent: safe to re-run.
#
#     sudo ./install.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/chatpad"
CONFIG_DIR="/etc/chatpad"
SERVICE="chatpad.service"
USER="chatpad"
GROUP="chatpad"

log()  { printf '\033[1;32m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }

[[ $EUID -eq 0 ]] || { err "run with sudo:  sudo $0"; exit 1; }

# Bookworm and later keep the firmware files in /boot/firmware; older
# releases use /boot directly.
if [[ -f /boot/firmware/config.txt ]]; then
    CONFIG_TXT=/boot/firmware/config.txt
    CMDLINE_TXT=/boot/firmware/cmdline.txt
else
    CONFIG_TXT=/boot/config.txt
    CMDLINE_TXT=/boot/cmdline.txt
fi

# ---------------------------------------------------------------------------
# 1. Dependencies
# ---------------------------------------------------------------------------
need_pkgs=()
python3 -c "import serial" 2>/dev/null || need_pkgs+=(python3-serial)
python3 -c "import evdev"  2>/dev/null || need_pkgs+=(python3-evdev)
if ((${#need_pkgs[@]})); then
    log "installing packages: ${need_pkgs[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${need_pkgs[@]}"
else
    log "python deps already present"
fi

# ---------------------------------------------------------------------------
# 2. System user/group
# ---------------------------------------------------------------------------
if ! getent group "$GROUP" >/dev/null; then
    log "creating group $GROUP"
    groupadd --system "$GROUP"
fi
if ! id "$USER" >/dev/null 2>&1; then
    log "creating user $USER"
    useradd --system --gid "$GROUP" --no-create-home --shell /usr/sbin/nologin "$USER"
fi

# ---------------------------------------------------------------------------
# 3. Files
# ---------------------------------------------------------------------------
log "installing driver to $INSTALL_DIR"
install -d -m 0755 "$INSTALL_DIR" "$CONFIG_DIR"
install -m 0755 "$SCRIPT_DIR/chatpad_driver.py" "$INSTALL_DIR/chatpad_driver.py"
install -m 0755 "$SCRIPT_DIR/chatpad_tui.py"    "$INSTALL_DIR/chatpad_tui.py"
install -m 0755 "$SCRIPT_DIR/chatpad_remap.py"  "$INSTALL_DIR/chatpad_remap.py"
install -m 0644 "$SCRIPT_DIR/README.md"         "$INSTALL_DIR/README.md"
rm -rf "$INSTALL_DIR/__pycache__"

# Keep an existing config; ship the default alongside it for reference.
if [[ -f "$CONFIG_DIR/config.json" ]]; then
    log "keeping existing $CONFIG_DIR/config.json (default saved as config.json.default)"
    install -m 0644 "$SCRIPT_DIR/config.json" "$CONFIG_DIR/config.json.default"
else
    log "installing default config to $CONFIG_DIR/config.json"
    install -m 0644 "$SCRIPT_DIR/config.json" "$CONFIG_DIR/config.json"
fi

# ---------------------------------------------------------------------------
# 4. Pi serial configuration
# ---------------------------------------------------------------------------
reboot_needed=0

add_config_line() {
    local line="$1"
    if ! grep -qxF "$line" "$CONFIG_TXT"; then
        log "adding to $CONFIG_TXT: $line"
        echo "$line" >> "$CONFIG_TXT"
        reboot_needed=1
    fi
}
if [[ -f "$CONFIG_TXT" ]]; then
    add_config_line "enable_uart=1"
    add_config_line "dtoverlay=disable-bt"
else
    warn "$CONFIG_TXT not found; add 'enable_uart=1' and 'dtoverlay=disable-bt' yourself."
fi

# Stop the login getty from claiming the UART.
for unit in serial-getty@ttyAMA0.service serial-getty@ttyS0.service getty@ttyAMA0.service; do
    if systemctl is-enabled "$unit" >/dev/null 2>&1; then
        log "disabling $unit"
        systemctl disable --now "$unit" 2>/dev/null || true
    fi
done
systemctl mask serial-getty@ttyAMA0.service >/dev/null 2>&1 || true

# Warn if a serial console is still on the kernel cmdline.
if grep -qE "console=(serial0|ttyAMA0|ttyS0)" "$CMDLINE_TXT" 2>/dev/null; then
    warn "$CMDLINE_TXT still routes a console to the serial port."
    warn "  Remove the 'console=serial0,...' term, or run:"
    warn "  raspi-config -> Interface Options -> Serial Port"
    warn "  (No to login shell, Yes to enable the serial hardware)."
fi

# ---------------------------------------------------------------------------
# 5. Device permissions
# ---------------------------------------------------------------------------
log "installing udev rule"
install -m 0644 "$SCRIPT_DIR/99-chatpad.rules" /etc/udev/rules.d/99-chatpad.rules
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=tty 2>/dev/null || true

# /dev/uinput is a static node udev does not manage; tmpfiles.d sets it at boot.
log "installing tmpfiles.d entry for /dev/uinput"
cat > /etc/tmpfiles.d/chatpad.conf <<EOF
z /dev/uinput   0660 root $GROUP -
z /dev/ttyAMA0  0660 root $GROUP -
EOF
systemd-tmpfiles --create /etc/tmpfiles.d/chatpad.conf 2>/dev/null || true
for dev in /dev/uinput /dev/ttyAMA0 /dev/ttyS0; do
    [[ -e "$dev" ]] && chown root:"$GROUP" "$dev" && chmod 0660 "$dev"
done

# ---------------------------------------------------------------------------
# 6. systemd service (+ drop-in when transmit is bit-banged on a GPIO)
# ---------------------------------------------------------------------------
log "installing systemd service"
install -m 0644 "$SCRIPT_DIR/chatpad.service" /etc/systemd/system/$SERVICE

DROPIN_DIR=/etc/systemd/system/chatpad.service.d
tx_gpio="$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get("tx_gpio"); print("" if v is None else v)' \
           "$CONFIG_DIR/config.json" 2>/dev/null || true)"
if [[ -n "$tx_gpio" ]]; then
    log "config sets tx_gpio=$tx_gpio: granting /dev/gpiomem and CAP_SYS_NICE to the service"
    getent group gpio >/dev/null || groupadd --system gpio
    install -d -m 0755 "$DROPIN_DIR"
    cat > "$DROPIN_DIR/tx-gpio.conf" <<'EOF'
# Written by install.sh because /etc/chatpad/config.json sets "tx_gpio".
[Service]
SupplementaryGroups=gpio
AmbientCapabilities=CAP_SYS_NICE
Nice=-10
EOF
else
    rm -f "$DROPIN_DIR/tx-gpio.conf"
    rmdir "$DROPIN_DIR" 2>/dev/null || true
fi

systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

# ---------------------------------------------------------------------------
# 7. chatpad-map shortcut + scoped passwordless sudo for it
# ---------------------------------------------------------------------------
log "installing 'chatpad-map' shortcut"
cat > /usr/local/bin/chatpad-map <<'EOF'
#!/bin/sh
# Launch the Chatpad keymap editor (needs root for the UART and /etc/chatpad).
exec sudo /usr/bin/python3 /opt/chatpad/chatpad_tui.py "$@"
EOF
chmod 0755 /usr/local/bin/chatpad-map

CALLER="${SUDO_USER:-}"
if [[ -n "$CALLER" && "$CALLER" != "root" ]]; then
    printf '%s ALL=(root) NOPASSWD: /usr/bin/python3 %s/chatpad_tui.py\n' \
        "$CALLER" "$INSTALL_DIR" > /etc/sudoers.d/chatpad-map
    chmod 0440 /etc/sudoers.d/chatpad-map
    log "chatpad-map runs without a password for $CALLER (scoped to the editor only)"
else
    warn "no non-root caller detected; 'chatpad-map' will ask for a password."
fi

# ---------------------------------------------------------------------------
# 8. Status / next steps
# ---------------------------------------------------------------------------
echo
echo "================== Chatpad driver install =================="
if [[ -e /dev/serial0 ]]; then
    log "/dev/serial0 present: $(readlink -f /dev/serial0)"
else
    warn "/dev/serial0 is NOT present. The UART config above needs a reboot;"
    warn "reboot, then re-run:  sudo $SCRIPT_DIR/install.sh"
fi
echo
systemctl --no-pager --lines=0 status "$SERVICE" 2>/dev/null | head -5 || true
echo
if [[ $reboot_needed -eq 1 ]]; then
    warn "$CONFIG_TXT was modified -- reboot to bring the UART online:  sudo reboot"
fi
echo "Check:  sudo journalctl -u chatpad -f   (then type on the Chatpad)"
echo "Remap:  chatpad-map   (stop the service first: sudo systemctl stop chatpad)"
echo "============================================================="
