#!/bin/bash
# Shared deploy script — used by both release.yml and deploy.yml
# Expects env vars: BOT_MODE, BANKROLL, POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER

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

# Build with version
sudo docker build --build-arg VERSION=$VERSION -t poly-bot:$VERSION -t poly-bot:latest .

# Restart
sudo docker stop poly-bot 2>/dev/null || true
sudo docker rm poly-bot 2>/dev/null || true
sudo docker run -d --restart=always \
  -v $(pwd)/config.yaml:/app/config.yaml \
  -v $(pwd)/data:/app/data \
  --name poly-bot \
  poly-bot:$VERSION --${BOT_MODE:-paper}

sleep 5
sudo docker logs --tail 10 poly-bot
echo "Deploy complete: $VERSION"
