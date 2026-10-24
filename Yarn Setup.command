#!/bin/bash
# Double-click this. It lives in the Yarn folder on purpose — nobody should
# have to open bin/ or a terminal.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# GitHub ZIP downloads are quarantined. Remove only that flag. Stripping
# every attribute is what made a signed app look damaged.
xattr -dr com.apple.quarantine "$ROOT" 2>/dev/null || true

# The release zip keeps the program in a hidden folder. A checkout keeps it here.
# Moving only this command onto the Desktop leaves the program behind.
LAUNCH=""
if [[ -x "$ROOT/.yarn/bin/yarn-setup" ]]; then
  LAUNCH="$ROOT/.yarn/bin/yarn-setup"
elif [[ -x "$ROOT/yarn/bin/yarn-setup" ]]; then
  LAUNCH="$ROOT/yarn/bin/yarn-setup"
elif [[ -x "$ROOT/bin/yarn-setup" ]]; then
  LAUNCH="$ROOT/bin/yarn-setup"
fi
if [[ -z "$LAUNCH" ]]; then
  osascript <<'EOF' || true
display dialog "This folder is incomplete. Unzip the Yarn download again, and run Yarn Setup.command from inside that folder. Do not move the command out on its own." buttons {"OK"} default button 1 with title "Yarn Setup"
EOF
  exit 0
fi
exec "$LAUNCH" "$@"
