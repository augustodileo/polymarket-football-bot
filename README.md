# Polymarket Football Bot

Automated football betting bot for [Polymarket](https://polymarket.com) prediction markets. Monitors live matches across 10+ leagues, detects edges using a probability model, and places bets with Kelly criterion position sizing.

**Paper trading results: 10W-0L, +$1,041 on $10K bankroll in first 24 hours.**

## How It Works

```
1. SCAN        Discover all football matches on Polymarket (EPL, La Liga, Serie A, etc.)
2. SCHEDULE    Flag tier mismatches (Bayern vs relegation team) for pre-match bets
3. MONITOR     Track live matches via Polymarket API (score, minute, period)
4. DECIDE      At minute 80+, compute probability from score + clock, compare to market price
5. CHECK       Verify orderbook liquidity (with retries and partial fills)
6. EXECUTE     Place bet if edge > threshold (paper or live)
7. RESOLVE     When match ends, calculate P&L, redeem winnings, update bankroll
```

## Strategy

**Standard (minute 80+):**
- Goal diff >= 2: buy the leading team to win
- Goal diff = 1: buy "losing team NOT to win" (covers draw + leader wins)
- Tied: buy "weaker side NOT to win"
- Only trade if edge > 2% vs Polymarket price
- Position sized with 25% Kelly criterion

**Tier Mismatch (pre-match):**
- When favorite >= 70% AND underdog <= 15%
- Bet NO on the weak team 30 min before kickoff
- Better prices than waiting until in-game

## Quick Start

### Requirements
- macOS/Linux
- [uv](https://docs.astral.sh/uv/) (`brew install uv`)

### Setup
```bash
git clone https://github.com/augustodileo/polymarket-football-bot.git
cd polymarket-football-bot
cp config.example.yaml config.yaml
# Edit config.yaml if needed
./run.sh --paper
```

No API keys needed. Uses Polymarket's free public APIs.

### Modes
```bash
./run.sh --paper        # simulated, state persists
./run.sh --live         # real money (needs wallet key in config)
./run.sh --ephemeral    # throwaway, wiped on start
```

## Project Structure

```
src/                    Source code
  main.py               Orchestrator
  engine.py             Decision engine
  stats.py              Data types
  analyze.py            Performance analysis
tests/                  163 tests, 87% coverage
.github/
  workflows/            CI + Release/Deploy + Smoke test
  deploy.sh             Shared deploy script
config.example.yaml     Template config (no secrets)
Dockerfile              Container image
run.sh                  Local launcher
```

## Testing

```bash
uv run pytest tests/ -v                                  # run all tests
uv run pytest tests/ --cov=. --cov-report=term-missing   # with coverage
```

CI runs on every push. Coverage must stay above 80%.

## Docker

```bash
docker build -t poly-bot .
docker run -v $(pwd)/config.yaml:/app/config.yaml -v $(pwd)/data:/app/data poly-bot --paper
```

Config is NOT baked into the image — injected via volume mount at runtime.

## CI/CD

| Event | What happens |
|-------|-------------|
| Every push | Tests + coverage + Docker build |
| `git tag v1.3.0 && git push --tags` | Tests → GitHub Release → Deploy to GCP → Smoke test |
| Manual (GitHub Actions UI) | Deploy to GCP → Smoke test |

Version comes from git tags. No version in any file.

## Analyzing Results

```bash
uv run src/analyze.py               # all paper trades
uv run src/analyze.py --today       # today only
uv run src/analyze.py --live        # live trades
uv run src/analyze.py --days        # list trade files
uv run src/analyze.py --wipe-paper  # reset paper data
```

## Live Trading

1. Create a **dedicated Polymarket wallet** (never your main)
2. Fund with USDC on Polygon
3. Add to GitHub Secrets: `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`
4. Set GitHub Variable: `BOT_MODE` to `live`
5. Push a new tag — deploys automatically in live mode

## Data

```
data/
  paper/                 Paper mode (safe to delete)
    state.json            Open positions + cumulative P&L
    trades_YYYY-MM-DD.jsonl  Daily trade logs
  live/                  Live mode (real money — don't delete)
    state.json
    trades_YYYY-MM-DD.jsonl
```

Paper and live data never mix.

## Disclaimer

For educational and paper trading purposes. Polymarket's Terms of Service may restrict automated trading and/or access from certain jurisdictions. Use at your own risk. Past simulated performance does not guarantee future results.
