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

# SwiftBar's own menu-bar item is a question mark beside Yarn. The plugin is
# the icon, so the host's mark stays hidden.
if [[ -d /Applications/SwiftBar.app ]]; then
  defaults write com.ameba.SwiftBar HideSwiftBarIcon -bool true
fi

PLUGIN_DIR="${SWIFTBAR_PLUGIN_DIR:-$HOME/.swiftbar-plugins}"
mkdir -p "$PLUGIN_DIR"
PLUGIN="$PLUGIN_DIR/meeting.5s.sh"
# Outside the plugin folder. SwiftBar executes every file it finds there, and
# it marks them executable, so a png next to the script becomes a second item.
MENU_MARK="${YARN_SUPPORT:-$HOME/Library/Application Support/Yarn}/menu-mark.png"

# Paths are baked in at install time so the plugin stays pure shell. It runs
# every 5 seconds; starting a Python interpreter that often just to draw an
# icon would be wasteful.
sed -e "s|__MA_ROOT__|$MA_ROOT|g" \
    -e "s|__REC__|$REC|g" \
    -e "s|__MA_BIN__|$MA_BIN|g" \
    -e "s|__BREW_BIN__|$BREW_BIN|g" \
    -e "s|__MENU_MARK__|$MENU_MARK|g" \
    "$REPO_DIR/ui/meeting.5s.sh.template" > "$PLUGIN"
chmod +x "$PLUGIN"
# Menu bar icon. SwiftBar tints a black template. The bitmap is 3x the
# 14-point menu-bar height so the yarn stays sharp, with
# the black around the yarn left transparent.
swiftc -o "$PLUGIN_DIR/.menu-mark" -framework AppKit - <<'SWIFT' || true
import AppKit
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let mark = NSImage(contentsOf: url)!
guard let fileRep = NSBitmapImageRep(data: try! Data(contentsOf: url)) else {
    fputs("could not read the toolbar mark\n", stderr)
    exit(1)
}
let srcW = fileRep.pixelsWide
let srcH = fileRep.pixelsHigh
func bitmap(_ w: Int, _ h: Int) -> NSBitmapImageRep {
    NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    )!
}
let probe = bitmap(srcW, srcH)
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: probe)
NSColor.black.setFill()
NSRect(x: 0, y: 0, width: srcW, height: srcH).fill()
mark.draw(in: NSRect(x: 0, y: 0, width: srcW, height: srcH), from: .zero, operation: .sourceOver, fraction: 1)
NSGraphicsContext.restoreGraphicsState()
guard let pp = probe.bitmapData else { exit(1) }
let prow = probe.bytesPerRow
var minX = srcW, minY = srcH, maxX = 0, maxY = 0
for y in 0..<srcH {
    for x in 0..<srcW {
        let i = y * prow + x * 4
        let lum = 0.2126 * Double(pp[i]) + 0.7152 * Double(pp[i+1]) + 0.0722 * Double(pp[i+2])
        if lum > 30 {
            minX = min(minX, x); minY = min(minY, y); maxX = max(maxX, x); maxY = max(maxY, y)
        }
    }
}
let contentW = maxX - minX + 1
let contentH = maxY - minY + 1
let crop = bitmap(contentW, contentH)
guard let cp = crop.bitmapData else { exit(1) }
let crow = crop.bytesPerRow
for y in 0..<contentH {
    for x in 0..<contentW {
        let s = (minY + y) * prow + (minX + x) * 4
        let d = y * crow + x * 4
        cp[d] = pp[s]; cp[d+1] = pp[s+1]; cp[d+2] = pp[s+2]; cp[d+3] = 255
    }
}
// 14 points tall, drawn at 3x. SwiftBar uses the image's point size, so the
// icon stays the same size in the menu bar and the extra pixels keep the
// yarn edges smooth on a Retina display.
let pointH = 14.0
let scale = 3
let targetH = Int(pointH) * scale
let targetW = max(1, Int((Double(contentW) / Double(contentH) * Double(targetH)).rounded()))
let out = bitmap(targetW, targetH)
NSGraphicsContext.saveGraphicsState()
let ctx = NSGraphicsContext(bitmapImageRep: out)!
ctx.imageInterpolation = .high
NSGraphicsContext.current = ctx
NSColor.clear.setFill()
NSRect(x: 0, y: 0, width: targetW, height: targetH).fill()
let cropped = NSImage(size: NSSize(width: contentW, height: contentH))
cropped.addRepresentation(crop)
cropped.draw(in: NSRect(x: 0, y: 0, width: targetW, height: targetH), from: .zero, operation: .sourceOver, fraction: 1)
NSGraphicsContext.restoreGraphicsState()
guard let dest = out.bitmapData else { exit(1) }
let drow = out.bytesPerRow
for y in 0..<targetH {
    for x in 0..<targetW {
        let i = y * drow + x * 4
        let lum = min(1, (0.2126 * Double(dest[i]) + 0.7152 * Double(dest[i+1]) + 0.0722 * Double(dest[i+2])) / 255)
        let ink = lum < 0.02 ? 0.0 : min(1, (lum - 0.02) / 0.55)
        dest[i] = 0
        dest[i + 1] = 0
        dest[i + 2] = 0
        dest[i + 3] = UInt8(ink * 255)
    }
}
out.size = NSSize(width: Double(targetW) / Double(scale), height: pointH)
try! out.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
SWIFT
mkdir -p "$(dirname "$MENU_MARK")"
if [[ -x "$PLUGIN_DIR/.menu-mark" ]]; then
  "$PLUGIN_DIR/.menu-mark" "$REPO_DIR/meeting_archive/assets/yarn-toolbar.png" "$MENU_MARK"
  rm -f "$PLUGIN_DIR/.menu-mark"
fi
rm -f "$PLUGIN_DIR/yarn-mark.png"
info "plugin written to $PLUGIN"
info "Stop/Start invoke: $REC"

# The Dock icon is the place you look. The menu bar stays as the quick control.
DOCK_OK=0
if [[ -x "$REPO_DIR/bin/yarn-dock" ]]; then
  info "installing the Dock app"
  if MA_BIN="$MA_BIN" YARN_REC="$REC" MA_ROOT="$MA_ROOT" "$REPO_DIR/bin/yarn-dock" --install; then
    DOCK_OK=1
  else
    info "Dock app was not installed"
  fi
fi

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
info "recorder loaded. It stays idle until you start a recording"

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
info "watcher loaded. It will now start at login"

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
if [[ "$DOCK_OK" == "1" ]]; then
  cat <<EOF
  Yarn is in the Dock. Click it to record, watch progress, name voices,
  open the daily brief, or ask about your meetings.

  The same controls are in the menu bar. Open SwiftBar if that icon is
  missing and it asks for a plugin folder:
      $PLUGIN_DIR
EOF
else
  cat <<EOF
  The Dock app was not built. Use the menu bar until Xcode Command Line
  Tools are installed, then run this installer again.

  Open SwiftBar if the menu-bar icon is missing and it asks for a plugin folder:
      $PLUGIN_DIR
EOF
fi
cat <<EOF

  The first recording will ask for microphone permission. Allow it once and
  it is remembered; the request comes from the recorder, not from SwiftBar.

  To uninstall the background jobs:
      launchctl bootout gui/\$(id -u)/$REC_LABEL
      launchctl bootout gui/\$(id -u)/$LABEL
      launchctl bootout gui/\$(id -u)/$PRUNE_LABEL
EOF
