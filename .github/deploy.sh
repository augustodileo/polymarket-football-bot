#!/bin/bash
# Shared deploy script — used by release.yml and deploy.yml
# Expects env vars: BOT_MODE, BANKROLL, POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER
#                   GITHUB_TOKEN, GITHUB_REPO, PUSH_INTERVAL_SEC

set -e
cd ~/poly-bot

git fetch --tags
git pull origin main

VERSION=$(git describe --tags --always 2>/dev/null || echo "dev")
echo "Deploying version: $VERSION"

# Generate config from template
cp config.example.yaml config.yaml
if [ -n "$BANKROLL" ]; then
  sed -i 's|bankroll: 10000|bankroll: '"$BANKROLL"'|' config.yaml
fi
if [ -n "$POLYMARKET_PRIVATE_KEY" ]; then
  sed -i 's|# polymarket_private_key: ""|polymarket_private_key: "'"$POLYMARKET_PRIVATE_KEY"'"|' config.yaml
fi
if [ -n "$POLYMARKET_FUNDER" ]; then
  sed -i 's|# polymarket_funder: ""|polymarket_funder: "'"$POLYMARKET_FUNDER"'"|' config.yaml
fi

# Export env vars for docker-compose
export VERSION
export BOT_MODE="${BOT_MODE:-paper}"
export GITHUB_TOKEN="${GITHUB_TOKEN:-}"
export GITHUB_REPO="${GITHUB_REPO:-}"
export PUSH_INTERVAL_SEC="${PUSH_INTERVAL_SEC:-300}"

# Stop and remove any old standalone containers (from pre-compose setup)
sudo docker stop poly-bot poly-dash 2>/dev/null || true
sudo docker rm poly-bot poly-dash 2>/dev/null || true

# Build and restart both containers via compose
sudo -E docker compose build
sudo -E docker compose down
sudo -E docker compose up -d

sleep 5
echo "=== Bot logs ==="
sudo docker logs --tail 10 poly-bot
echo ""
echo "=== Dashboard logs ==="
sudo docker logs --tail 5 poly-dash
echo ""
echo "Deploy complete: $VERSION"
