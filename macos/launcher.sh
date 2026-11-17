#!/bin/bash
# War Room.app launcher: opens a dedicated Ghostty instance running the War Room
# TUI and waits for it. Closing the window quits that Ghostty instance (and the
# TUI with it), which in turn ends this launcher, so the app fully shuts down.

# Lives in Contents/Resources (started by the Swift executable) or, as a
# fallback, is the executable itself in Contents/MacOS.
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/war-room-session.sh" ]; then RES="$HERE"; else RES="$(cd "$HERE/../Resources" && pwd)"; fi
GHOSTTY="/Applications/Ghostty.app"
[ -d "$GHOSTTY" ] || GHOSTTY="$HOME/Applications/Ghostty.app"

if [ ! -d "$GHOSTTY" ]; then
  /usr/bin/osascript -e 'display alert "War Room needs Ghostty" message "Install Ghostty (brew install --cask ghostty), then open War Room again. Or run: cd __REPO__ && uv run python -m backend.tui"' >/dev/null
  exit 1
fi

# Extra arguments (e.g. `open -a "War Room" --args --fake`, or the
# `--context PATH` the app adds for a dropped folder) go to the TUI.
args=()
for a in "$@"; do [[ "$a" == -psn_* ]] || args+=("$a"); done

exec /usr/bin/open -n -W -a "$GHOSTTY" --args \
  --config-default-files=true \
  --title="War Room" \
  --macos-icon=custom \
  --macos-custom-icon="$RES/AppIcon.icns" \
  --quit-after-last-window-closed=true \
  --confirm-close-surface=false \
  --window-save-state=never \
  --wait-after-command=false \
  --background=1F1F1F \
  --foreground=FFFFFF \
  --background-opacity=1 \
  --window-width=130 \
  --window-height=44 \
  -e "$RES/war-room-session.sh" "${args[@]}"
