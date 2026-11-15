#!/bin/bash
# Runs inside the War Room terminal window (started by War Room.app).
#
# The TUI kills its own CLI subprocesses on hangup; as a safety net this
# wrapper also sweeps any process that still carries this session's marker
# environment variable once the TUI is gone, so closing the window leaves
# no python / codex / claude / grok processes behind.

REPO="${WARROOM_REPO:-__REPO__}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
unset NO_COLOR
export COLORTERM=truecolor
export WARROOM_SESSION_TOKEN="warroom-$$-$RANDOM$RANDOM"

child=""

sweep() {
  [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
  local pids
  pids=$(ps -E -ww -o pid=,command= -U "$(id -u)" 2>/dev/null \
    | grep -F "WARROOM_SESSION_TOKEN=$WARROOM_SESSION_TOKEN" | grep -v -e grep -e ' ps ' \
    | awk -v me="$$" '$1 != me {print $1}')
  if [ -n "$pids" ]; then
    kill -TERM $pids 2>/dev/null
    sleep 0.5
    kill -KILL $pids 2>/dev/null
  fi
}

on_signal() {
  sweep
  exit 129
}

trap on_signal HUP TERM INT
trap sweep EXIT

cd "$REPO" 2>/dev/null || { echo "War Room repo not found at $REPO"; read -r -p "Press Enter to close."; exit 1; }

if ! uv sync --quiet >/dev/null 2>&1; then
  echo "uv sync failed in $REPO; trying to start anyway…"
fi

# Run the TUI in the background (stdin kept on the terminal) so the traps
# above fire immediately on hangup instead of after the child exits.
.venv/bin/python -m backend.tui "$@" <&0 &
child=$!
wait "$child"
status=$?
child=""
if [ "$status" -ne 0 ] && [ "$status" -lt 128 ]; then
  echo
  echo "War Room exited with status $status."
  read -r -t 30 -p "Press Enter to close this window." || true
fi
exit "$status"
