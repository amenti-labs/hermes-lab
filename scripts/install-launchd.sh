#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "launchd installation is only supported on macOS." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_ROOT="${HERMES_LAB_DATA_ROOT:-./lab-data}"
TARGET_DIR="$HOME/Library/LaunchAgents"
LOAD_AGENTS=1

if [[ "${1:-}" == "--no-load" ]]; then
  LOAD_AGENTS=0
fi

mkdir -p "$TARGET_DIR"

render_plist() {
  local src="$1"
  local dst="$2"
  sed \
    -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
    -e "s|__DATA_ROOT__|$DATA_ROOT|g" \
    "$src" > "$dst"
  plutil -lint "$dst" >/dev/null
}

RUN_ONCE_DST="$TARGET_DIR/com.example.hermes-lab.run-once.plist"
DIGEST_DST="$TARGET_DIR/com.example.hermes-lab.digest.plist"
WEEKLY_DST="$TARGET_DIR/com.example.hermes-lab.weekly-digest.plist"

render_plist "$REPO_ROOT/config/com.example.hermes-lab.run-once.plist" "$RUN_ONCE_DST"
render_plist "$REPO_ROOT/config/com.example.hermes-lab.digest.plist" "$DIGEST_DST"
render_plist "$REPO_ROOT/config/com.example.hermes-lab.weekly-digest.plist" "$WEEKLY_DST"

if [[ "$LOAD_AGENTS" -eq 1 ]]; then
  for plist in "$RUN_ONCE_DST" "$DIGEST_DST" "$WEEKLY_DST"; do
    launchctl bootout "gui/$UID" "$plist" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$UID" "$plist"
  done
fi

echo "Installed launchd agents into $TARGET_DIR"
echo "  run-once: $RUN_ONCE_DST"
echo "  digest: $DIGEST_DST"
echo "  weekly digest: $WEEKLY_DST"

if [[ "$LOAD_AGENTS" -eq 1 ]]; then
  echo "Agents loaded for gui/$UID"
else
  echo "Rendered only. Load later with:"
  echo "  launchctl bootstrap gui/$UID $RUN_ONCE_DST"
  echo "  launchctl bootstrap gui/$UID $DIGEST_DST"
  echo "  launchctl bootstrap gui/$UID $WEEKLY_DST"
fi
