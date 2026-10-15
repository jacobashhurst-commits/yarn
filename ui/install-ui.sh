#!/usr/bin/env bash
#
# Installs the parts that remove the terminal from day-to-day use:
#
#   1. A SwiftBar menu bar item to start and stop recordings, and to say which
#      kind of meeting it is at the moment you start it.
#   2. A LaunchAgent running `ma watch` in the background, so transcription and
#      indexing happen without a terminal window open.
#
# Run once. It prints everything it does and asks before installing SwiftBar.

set -euo pipefail

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REC="$REPO_DIR/bin/rec"
[[ -x "$REC" ]] || die "$REC is missing or not executable"

command -v ma >/dev/null 2>&1 || die "'ma' is not on PATH -- finish the install in the README first"

BREW_BIN="$(dirname "$(command -v brew 2>/dev/null || echo /opt/homebrew/bin/brew)")"
MA_ROOT="$(ma rec-config | sed -n 's/^MA_ROOT="\(.*\)"$/\1/p')"
[[ -n "$MA_ROOT" ]] || die "could not read the archive root from 'ma rec-config'"
info "archive root: $MA_ROOT"
info "repo:         $REPO_DIR"
MA_BIN="$(command -v ma)"
# Asked for rather than derived from MA_BIN, which is usually a symlink into
# the virtualenv. The recorder has to be the virtualenv's own script so that it
# imports the project's sounddevice.
CAPTURE_BIN="$(ma rec-config | sed -n 's/^MA_CAPTURE_BIN="\(.*\)"$/\1/p')"
[[ -x "$CAPTURE_BIN" ]] || die "ma-capture is missing; re-run 'pip install -e .'"

# launchd cannot read Downloads, Desktop, or Documents (macOS TCC). A recorder
# whose venv lives there dies on pyvenv.cfg with "Operation not permitted",
# and the menu bar only shows "exited before it started recording".
blocked=""
case "$REPO_DIR" in
  */Downloads/*|*/Desktop/*|*/Documents/*) blocked="$REPO_DIR" ;;
esac
case "$CAPTURE_BIN" in
  */Downloads/*|*/Desktop/*|*/Documents/*) blocked="$CAPTURE_BIN" ;;
esac
if [[ -n "$blocked" ]]; then
  die "Yarn cannot run from Downloads, Desktop, or Documents ($blocked). macOS blocks the recorder there. Re-run Yarn Setup."
fi

# ---------------------------------------------------------------------------
say "1/4  SwiftBar (menu bar UI)"

if [[ -d "/Applications/SwiftBar.app" ]]; then
  info "already installed"
elif [[ "${YARN_SETUP_NONINTERACTIVE:-}" == "1" ]]; then
  if command -v brew >/dev/null 2>&1; then
    info "installing SwiftBar with Homebrew"
    brew install --cask swiftbar
  else
    info "SwiftBar is not installed; the menu bar icon is skipped."
    info "Background jobs still run. Install SwiftBar later and re-run this script."
  fi
else
  info "SwiftBar is a free, open-source menu bar host for shell scripts."
  info "It is the only new dependency, and it is what gives you the record button."
  read -r -p "  Install it with Homebrew now? [y/N] " reply
  case "$reply" in
    [yY]*) brew install --cask swiftbar ;;
    *) die "skipped -- the menu bar UI needs SwiftBar. Re-run when ready." ;;
  esac
fi

PLUGIN_DIR="${SWIFTBAR_PLUGIN_DIR:-$HOME/.swiftbar-plugins}"
mkdir -p "$PLUGIN_DIR"
PLUGIN="$PLUGIN_DIR/meeting.5s.sh"

# Paths are baked in at install time so the plugin stays pure shell. It runs
# every 5 seconds; starting a Python interpreter that often just to draw an
# icon would be wasteful.
sed -e "s|__MA_ROOT__|$MA_ROOT|g" \
    -e "s|__REC__|$REC|g" \
    -e "s|__MA_BIN__|$MA_BIN|g" \
    -e "s|__BREW_BIN__|$BREW_BIN|g" \
    "$REPO_DIR/ui/meeting.5s.sh.template" > "$PLUGIN"
chmod +x "$PLUGIN"
info "plugin written to $PLUGIN"
info "Stop/Start invoke: $REC"

# Keep a brew-prefix `rec` symlink when `ma` lives there, so an older plugin
# that still points at /opt/homebrew/bin/rec keeps working after reinstall.
MA_PREFIX="$(cd "$(dirname "$MA_BIN")" && pwd)"
if [[ -w "$MA_PREFIX" && ! -e "$MA_PREFIX/rec" ]]; then
  ln -sf "$REC" "$MA_PREFIX/rec"
  info "symlinked $MA_PREFIX/rec -> $REC"
elif [[ -L "$MA_PREFIX/rec" ]]; then
  ln -sf "$REC" "$MA_PREFIX/rec"
fi

# ---------------------------------------------------------------------------
say "2/4  Recorder (LaunchAgent)"

# The capture runs as a launchd job rather than as a child of whatever was
# clicked. On macOS, microphone permission belongs to the "responsible
# process", which for a SwiftBar plugin is SwiftBar -- and SwiftBar's bundle
# declares no NSMicrophoneUsageDescription, so it can never hold that
# permission and never appears in the Microphone list. macOS does not refuse;
# it returns silence. Recording under launchd makes this tool responsible for
# itself, so the permission is asked for once and actually granted.
REC_LABEL="com.meetingarchive.recorder"
REC_PLIST="$HOME/Library/LaunchAgents/$REC_LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$MA_ROOT/logs"

cat > "$REC_PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$REC_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$CAPTURE_BIN</string>
    <string>$MA_ROOT/.capture-request</string>
  </array>
  <key>RunAtLoad</key><false/>
  <key>KeepAlive</key><false/>
  <!-- Audio capture must not be background-throttled. Under launchd's default
       background scheduling a sustained recording once received barely a
       quarter of its sample buffers. -->
  <key>ProcessType</key><string>Interactive</string>
  <key>ThrottleInterval</key><integer>1</integer>
  <key>StandardOutPath</key><string>$MA_ROOT/logs/recorder.log</string>
  <key>StandardErrorPath</key><string>$MA_ROOT/logs/recorder.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$BREW_BIN:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
PLIST_EOF

launchctl bootout "gui/$(id -u)/$REC_LABEL" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$REC_PLIST"
info "recorder loaded — it stays idle until you start a recording"

# ---------------------------------------------------------------------------
say "3/4  Background watcher (LaunchAgent)"

LABEL="com.meetingarchive.watcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$MA_ROOT/logs"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$MA_BIN</string>
    <string>watch</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$MA_ROOT/logs/watcher.out.log</string>
  <key>StandardErrorPath</key><string>$MA_ROOT/logs/watcher.err.log</string>
  <key>WorkingDirectory</key><string>$REPO_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$BREW_BIN:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>ANONYMIZED_TELEMETRY</key><string>false</string>
  </dict>
</dict>
</plist>
PLIST_EOF
info "wrote $PLIST"

# bootout first so re-running the installer reloads cleanly rather than
# erroring on an already-loaded label.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
# launchd can keep the old job teardown in flight briefly and return EIO when
# bootstrap follows bootout immediately. A short pause makes reinstall
# reliable; retry once because leaving the watcher unloaded would strand the
# next meeting in the inbox.
sleep 1
if ! launchctl bootstrap "gui/$(id -u)" "$PLIST"; then
  sleep 2
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
fi
info "watcher loaded — it will now start at login"

# ---------------------------------------------------------------------------
say "4/4  Daily audio prune"

PRUNE_LABEL="com.meetingarchive.prune"
PRUNE_PLIST="$HOME/Library/LaunchAgents/$PRUNE_LABEL.plist"
cat > "$PRUNE_PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$PRUNE_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$MA_BIN</string>
    <string>prune</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$MA_ROOT/logs/prune.log</string>
  <key>StandardErrorPath</key><string>$MA_ROOT/logs/prune.log</string>
</dict>
</plist>
PLIST_EOF
launchctl bootout "gui/$(id -u)/$PRUNE_LABEL" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PRUNE_PLIST"
info "prune scheduled daily at 09:00"

# ---------------------------------------------------------------------------
say "Done"
cat <<EOF
  Open SwiftBar. If it asks for a plugin folder, choose:
      $PLUGIN_DIR

  A ⏺ icon appears in your menu bar. Click it to start a recording and pick
  which kind of meeting it is. It turns red with a running timer while
  recording, and the same menu stops it.

  The first recording will ask for microphone permission. Allow it once and
  it is remembered; the request comes from the recorder, not from SwiftBar.

  To uninstall the background jobs:
      launchctl bootout gui/\$(id -u)/$REC_LABEL
      launchctl bootout gui/\$(id -u)/$LABEL
      launchctl bootout gui/\$(id -u)/$PRUNE_LABEL
EOF
