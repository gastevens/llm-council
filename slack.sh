#!/bin/bash
# War Room Slack bot launcher (Socket Mode, no public URL needed).
#   ./slack.sh                        connect to Slack (needs SLACK_BOT_TOKEN + SLACK_APP_TOKEN in .env)
#   ./slack.sh --dry-run "Question"   run the council and print what would be posted
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PYTHONUNBUFFERED=1

if [[ "${1:-}" != "--dry-run" && "${1:-}" != "--fake" && "${1:-}" != "-h" && "${1:-}" != "--help" ]]; then
  for var in SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
    if [[ -z "${!var:-}" ]] && ! grep -Eq "^${var}=.+" .env 2>/dev/null; then
      echo "Missing $var. Add it to $(pwd)/.env (see .env.example and the README's Slack section)." >&2
      exit 2
    fi
  done
fi

exec uv run python -m backend.slack_bot "$@"
