#!/bin/bash
# Double-click this. It lives in the Yarn folder on purpose — nobody should
# have to open bin/ or a terminal.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# GitHub ZIP downloads are quarantined. Once macOS has allowed this file to
# run, strip that flag so the next click is quiet.
xattr -cr "$ROOT" 2>/dev/null || true

# The release zip keeps the program in a hidden folder. A checkout keeps it here.
if [[ -x "$ROOT/.yarn/bin/yarn-setup" ]]; then
  exec "$ROOT/.yarn/bin/yarn-setup" "$@"
fi
if [[ -x "$ROOT/yarn/bin/yarn-setup" ]]; then
  exec "$ROOT/yarn/bin/yarn-setup" "$@"
fi
exec "$ROOT/bin/yarn-setup" "$@"
