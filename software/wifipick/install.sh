#!/bin/sh
# Install wifipick: touch-friendly WiFi picker TUI, plus the `wifi` and
# `wifistatus` commands.
#
# Touch input comes from touchscroll (taps -> SGR mouse clicks, two-finger
# swipes -> SGR wheel). Without it the TUI still works from the keyboard.
#
#     sudo ./install.sh
set -eu

INSTALL_DIR=/opt/wifipick
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
CALLER=${SUDO_USER:-}

log() { echo "  $*"; }
warn() { echo "  WARNING: $*" >&2; }

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }

command -v nmcli >/dev/null 2>&1 || warn "nmcli not found; wifipick needs NetworkManager (apt install network-manager)"
command -v iw    >/dev/null 2>&1 || [ -x /usr/sbin/iw ] || warn "iw not found; wifistatus will fall back to nmcli (apt install iw)"

install -d "$INSTALL_DIR"
install -m 0644 "$SCRIPT_DIR/wifipick.py"     "$INSTALL_DIR/wifipick.py"
install -m 0644 "$SCRIPT_DIR/wifipick_tui.py" "$INSTALL_DIR/wifipick_tui.py"
install -m 0644 "$SCRIPT_DIR/README.md"       "$INSTALL_DIR/README.md"
install -m 0755 "$SCRIPT_DIR/wifistatus"      /usr/local/bin/wifistatus
rm -rf "$INSTALL_DIR/__pycache__"
log "installed $INSTALL_DIR and /usr/local/bin/wifistatus"

cat > /usr/local/bin/wifi <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /opt/wifipick/wifipick_tui.py "$@"
EOF
chmod 0755 /usr/local/bin/wifi
log "installed /usr/local/bin/wifi"

# NetworkManager lets members of netdev manage connections without a
# password on Debian; otherwise wifipick falls back to `sudo -n nmcli`.
if [ -n "$CALLER" ] && [ "$CALLER" != root ] && getent group netdev >/dev/null \
   && ! id -nG "$CALLER" | grep -qw netdev; then
    usermod -aG netdev "$CALLER"
    log "added $CALLER to the netdev group (log out and back in)"
fi

if ! systemctl is-enabled touchscroll.service >/dev/null 2>&1; then
    warn "touchscroll.service is not enabled; the TUI will have no touch input (keyboard still works)."
fi

echo
echo "Done. Run 'wifi' from the console."
echo "  touch:  tap network = select, tap again = connect, two-finger swipe = scroll"
echo "  keys:   arrows or h/l to move, Enter/A connect, R rescan, Q quit"
echo "  bar:    wifistatus --once"
