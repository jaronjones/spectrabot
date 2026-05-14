#!/usr/bin/env bash
# Remove SpectraBot installation. Leaves config and state in place by default.
#
#   ./uninstall.sh            remove binaries + schedule, keep config/state/logs
#   ./uninstall.sh --purge    also remove ~/.spectrabot entirely
set -euo pipefail

PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

OS="$(uname -s)"
HOME_DIR="$HOME/.spectrabot"
BIN_LINK="$HOME/.local/bin/spectrabot"

echo ">> uninstalling SpectraBot (os: $OS)"

case "$OS" in
  Linux)
    systemctl --user disable --now spectrabot.timer 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/spectrabot.timer" \
          "$HOME/.config/systemd/user/spectrabot.service"
    systemctl --user daemon-reload || true
    ;;
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.spectrabot.scan.plist"
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    ;;
esac

rm -f "$BIN_LINK"

if [[ $PURGE -eq 1 ]]; then
  rm -rf "$HOME_DIR"
  echo ">> purged $HOME_DIR"
else
  # Remove only code; keep config + state + logs.
  rm -rf "$HOME_DIR/bin" "$HOME_DIR/lib"
  echo ">> removed $HOME_DIR/{bin,lib}; left config + state + logs in $HOME_DIR"
  echo "   (pass --purge to remove them too)"
fi
echo ">> done"
