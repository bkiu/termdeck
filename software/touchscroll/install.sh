#!/bin/sh
# Install touchscroll: touchscreen taps -> clicks, two-finger swipes -> scroll,
# delivered to whatever full-screen TUI is on the console.
#
#     sudo ./install.sh
set -eu

INSTALL_DIR=/opt/touchscroll
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
CALLER=${SUDO_USER:-}

log() { echo "  $*"; }
warn() { echo "  WARNING: $*" >&2; }

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }

# 0. Dependency.
if ! python3 -c "import evdev" 2>/dev/null; then
    log "installing python3-evdev"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-evdev
fi

# 1. gpm turns touches into console text selection (the highlight you get
#    when you drag on the screen) and never scrolls a TUI. Turn it off.
if systemctl is-enabled gpm.service >/dev/null 2>&1; then
    systemctl disable --now gpm.service 2>/dev/null || true
    log "disabled gpm (it only selected text, never scrolled)"
fi

# 2. Files.
install -d "$INSTALL_DIR"
install -m 0644 "$SCRIPT_DIR/touchscroll.py" "$INSTALL_DIR/touchscroll.py"
install -m 0755 "$SCRIPT_DIR/test_touch.py"  "$INSTALL_DIR/test_touch.py"
install -m 0644 "$SCRIPT_DIR/README.md"      "$INSTALL_DIR/README.md"
rm -rf "$INSTALL_DIR/__pycache__"
log "installed $INSTALL_DIR"

# 3. Let the installing user run test_touch.py without root. Debian puts
#    /dev/input/event* in the `input` group.
if [ -n "$CALLER" ] && [ "$CALLER" != root ] && ! id -nG "$CALLER" | grep -qw input; then
    usermod -aG input "$CALLER"
    log "added $CALLER to the input group (log out and back in for test_touch.py)"
fi

# 4. Service.
install -m 0644 "$SCRIPT_DIR/touchscroll.service" /etc/systemd/system/touchscroll.service
systemctl daemon-reload
systemctl enable touchscroll.service
systemctl restart touchscroll.service
log "enabled + started touchscroll.service"

echo
echo "Done. Two-finger swipes scroll the foreground TUI; single-finger taps click."
echo "  check:  journalctl -u touchscroll -f"
echo "  test:   /opt/touchscroll/test_touch.py   (dry run, prints what it would inject)"
