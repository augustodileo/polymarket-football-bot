#!/usr/bin/env bash
# Launch the Polymarket Football Bot
# Usage (one flag required):
#   ./run.sh --paper        # paper trading, state persists
#   ./run.sh --live         # real money
#   ./run.sh --ephemeral    # throwaway run, wiped on start

set -e
cd "$(dirname "$0")"

# Ensure uv is available
if ! command -v uv &> /dev/null; then
    echo "Error: 'uv' is not installed. Install with: brew install uv"
    exit 1
fi

# Sync dependencies (fast no-op if already synced)
uv sync --quiet 2>/dev/null || uv sync

# Write version from git tag
git describe --tags --always 2>/dev/null > VERSION || echo "dev" > VERSION

# Run the bot
exec uv run python src/main.py "$@"
