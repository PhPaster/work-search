#!/bin/bash
# One pipeline run, for cron/launchd. Logs go to data/logs/.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/logs
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
uv run jobradar run "$@" >> "data/logs/run-$(date +%Y-%m).log" 2>&1
