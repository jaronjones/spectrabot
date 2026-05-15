#!/usr/bin/env bash
# Install SpectraBot for the current user.
#
#   ./install.sh             install + enable scheduled scans
#   ./install.sh --no-enable install but don't enable the schedule
#   ./install.sh --uninstall remove everything (use uninstall.sh)
#
# All tool data lives under ~/.spectrabot/.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="$HOME/.spectrabot"
BIN_LINK="$HOME/.local/bin/spectrabot"

ENABLE_SCHEDULE=1
for arg in "$@"; do
  case "$arg" in
    --no-enable) ENABLE_SCHEDULE=0 ;;
    --uninstall) exec "$SRC_DIR/uninstall.sh" ;;
    -h|--help)
      sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

OS="$(uname -s)"
echo ">> installing SpectraBot (os: $OS)"

# ---- preflight ----
need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing required command: $1" >&2; exit 1; }
}
need_cmd python3
need_cmd gh

# The review engine is chosen in config.toml (claude / codex / opencode),
# which doesn't exist yet at preflight — so warn rather than fail.
if ! command -v claude >/dev/null 2>&1 \
   && ! command -v codex >/dev/null 2>&1 \
   && ! command -v opencode >/dev/null 2>&1; then
  echo "WARNING: no review engine found on PATH (claude / codex / opencode)." >&2
  echo "         Install the one you set as \`[engine] name\` before the first scan." >&2
fi

PY_MAJOR_MINOR="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "python3 >= 3.11 required (found $PY_MAJOR_MINOR; tomllib is stdlib starting 3.11)" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "WARNING: \`gh\` is not authenticated. Run \`gh auth login\` before the first scan." >&2
fi

# ---- copy files into ~/.spectrabot/ ----
mkdir -p "$HOME_DIR/bin" "$HOME_DIR/lib" "$HOME_DIR/state" "$HOME_DIR/logs"

install -m 0755 "$SRC_DIR/bin/spectrabot"          "$HOME_DIR/bin/spectrabot"
install -m 0644 "$SRC_DIR/lib/scan.py"             "$HOME_DIR/lib/scan.py"
install -m 0644 "$SRC_DIR/lib/review_prompt.md"    "$HOME_DIR/lib/review_prompt.md"

if [[ ! -f "$HOME_DIR/config.toml" ]]; then
  install -m 0644 "$SRC_DIR/config/config.example.toml" "$HOME_DIR/config.toml"
  echo ">> wrote starter config to $HOME_DIR/config.toml (edit it to add repos)"
else
  echo ">> existing config left in place at $HOME_DIR/config.toml"
fi

# ---- PATH symlink ----
mkdir -p "$(dirname "$BIN_LINK")"
ln -sf "$HOME_DIR/bin/spectrabot" "$BIN_LINK"
echo ">> linked $BIN_LINK -> $HOME_DIR/bin/spectrabot"

case ":$PATH:" in
  *":$(dirname "$BIN_LINK"):"*) ;;
  *) echo "WARNING: $(dirname "$BIN_LINK") is not in your PATH. Add it to your shell rc." >&2 ;;
esac

# ---- schedule ----
if [[ $ENABLE_SCHEDULE -eq 0 ]]; then
  echo ">> --no-enable given; skipping schedule install"
  exit 0
fi

case "$OS" in
  Linux)
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    install -m 0644 "$SRC_DIR/service/spectrabot.service" "$UNIT_DIR/spectrabot.service"
    install -m 0644 "$SRC_DIR/service/spectrabot.timer"   "$UNIT_DIR/spectrabot.timer"
    systemctl --user daemon-reload
    systemctl --user enable --now spectrabot.timer
    echo ">> systemd user timer installed and started"
    echo "   logs:   journalctl --user -u spectrabot.service -f"
    echo "   status: systemctl --user list-timers spectrabot.timer"
    if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
      echo "   NOTE: enable user lingering so the timer runs while you're logged out:"
      echo "         sudo loginctl enable-linger $USER"
    fi
    ;;
  Darwin)
    PLIST_DIR="$HOME/Library/LaunchAgents"
    PLIST_DST="$PLIST_DIR/com.spectrabot.scan.plist"
    mkdir -p "$PLIST_DIR"
    sed "s|__HOME__|$HOME|g" "$SRC_DIR/service/com.spectrabot.scan.plist" > "$PLIST_DST"
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    launchctl load "$PLIST_DST"
    echo ">> launchd agent installed and loaded"
    echo "   logs: tail -f $HOME_DIR/logs/scan.log"
    ;;
  *)
    echo "unsupported OS '$OS' — copy files manually or run spectrabot from cron." >&2
    exit 1
    ;;
esac

echo
echo ">> done. Next steps:"
echo "   1. Edit $HOME_DIR/config.toml and add repos under [github].repos"
echo "   2. Run a dry-run to confirm:  spectrabot --dry-run"
