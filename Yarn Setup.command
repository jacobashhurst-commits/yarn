#!/bin/bash
# Double-click this. It lives in the Yarn folder on purpose — nobody should
# have to open bin/ or a terminal.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# GitHub ZIP downloads are quarantined. Once macOS has allowed this file to
# run, strip that flag so the next click is quiet.
xattr -cr "$ROOT" 2>/dev/null || true

exec "$ROOT/bin/yarn-setup" "$@"
