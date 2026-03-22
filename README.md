# Polymarket Football Bot

Automated football betting bot for [Polymarket](https://polymarket.com) prediction markets. Monitors live matches across 10+ leagues, detects edges using historical base rates and market prices, and places bets with Kelly criterion position sizing.

## How It Works

```
1. SCAN        Discover all football matches on Polymarket (EPL, La Liga, Serie A, etc.)
2. SCHEDULE    Flag tier mismatches (Bayern vs relegation team) for pre-match bets
3. MONITOR     Track live matches via Polymarket API (score, minute, period)
4. DECIDE      At minute 80+, compute true probability vs market price
5. CHECK       Verify orderbook liquidity before placing
6. EXECUTE     Place bet if edge > threshold (paper or live)
7. RESOLVE     When match ends, calculate P&L, redeem winnings, update bankroll
```

## Strategy Rules

**Standard (minute 80+):**
- Goal diff >= 2: buy the leading team to win
- Goal diff = 1: buy "losing team NOT to win" (covers draw + leader wins)
- Tied: buy "weaker side NOT to win"
- Only trade if edge > 2% vs Polymarket price
- Position sized with 25% Kelly criterion

**Tier Mismatch (heavy favorite >= 65%):**
- Pre-match: bet NO on the weak team 30 min before kickoff
- In-game: trade from minute 70 with lower edge threshold (1.5%)

## Quick Start

### Requirements
- macOS (tested) or Linux
- [uv](https://docs.astral.sh/uv/) package manager (`brew install uv`)

### Setup

```bash
git clone <repo> && cd polymarket-football-bot

# First run — uv downloads Python 3.12 and all dependencies automatically
./run.sh
```

No API keys needed. The bot uses Polymarket's free public APIs for everything.

### Configuration

Edit `config.yaml`:

```yaml
# Starting bankroll for paper trading
bankroll: 10000

# Risk limits
risk:
  max_daily_loss: 500
  max_single_stake: 1000
  max_concurrent_positions: 5
  min_edge_pct: 2.0
  kelly_fraction: 0.25

# Tier mismatch — bet on heavy favorites pre-match
tier_mismatch:
  enabled: true
  min_favorite_prob: 0.65
  pre_match: true
  pre_match_bet_minutes_before: 30
```

### Running

```bash
./run.sh              # paper mode — simulates trades, tracks P&L
./run.sh --live       # real money — requires Polymarket wallet key in config.yaml
```

### What You See

```
21:00:00 [INFO] Bot started in PAPER mode | Bankroll: $10,000.00
21:00:00 [INFO] Monitoring 10 leagues | Trade from minute 80+
21:00:00 [INFO] Tier mismatch: ON | Favorite >= 65% -> pre-match + in-game from min 70+

21:00:01 [INFO] === DAILY SCAN: 2026-03-22 ===
21:00:01 [INFO]   Premier League: 6 matches
21:00:01 [INFO]     Arsenal FC vs. Brighton [KO 15:00 UTC]
21:00:01 [INFO]     ...
21:00:01 [INFO]   La Liga: 4 matches
21:00:01 [INFO]   Serie A: 5 matches
21:00:01 [INFO] === END SCAN (42 total across 12 leagues) ===

21:00:02 [INFO] [PRE-MATCH] Scheduled: Napoli vs Cagliari | Napoli 82% fav | Bet at 14:30 UTC

============================================================
  [PAPER] MATCH: CA Osasuna vs. Girona FC
  MINUTE: 80 | SCORE: 0-0
  SIGNAL: BUY
  REASON: Edge 4.4% on NO Will Girona FC win?
------------------------------------------------------------
  Market:     Will Girona FC win on 2026-03-21?
  Side:       NO
  True Prob:  90.9%
  Poly Price: 0.8650
  Edge:       +4.4%
  Kelly 25%:  8.1%
  Stake:      $808.24
  Shares:     934
  Win -> +$126.09
  Lose -> -$807.91
  EV:         $40.76
============================================================

************************************************************
  RESULT: WIN
  MATCH: CA Osasuna vs. Girona FC
  Final Score: 1-0
  Position: NO on "Will Girona FC win on 2026-03-21?"
  P&L: +126.09
  Session: 1W-0L | P&L: +126.09
************************************************************
```

## Project Structure

```
polymarket-football-bot/
  config.yaml        Settings (bankroll, risk, leagues, strategy)
  main.py            Orchestrator — the main loop
  engine.py          Decision engine — probability model, edge, Kelly sizing
  stats.py           Data types (MatchStats, BookmakerOdds)
  analyze.py         Trade performance analysis
  run.sh             Launcher script
  data/
    paper/           Paper mode state + daily trade logs
    live/            Live mode state + daily trade logs
    ephemeral/       Throwaway runs (wiped on start)
  tests/             Unit tests (134 tests, 80% coverage)
```

## Data Sources

All free, no API keys required:

| Source | What | Endpoint |
|--------|-------|----------|
| Polymarket Gamma API | Market discovery, prices, live scores | `gamma-api.polymarket.com` |
| Polymarket CLOB API | Orderbook, midpoint, liquidity | `clob.polymarket.com` |

## Testing

134 unit tests covering the decision engine, parsing, mismatch detection, outcome resolution, state persistence, liquidity checks, and trade analysis.

```bash
uv run pytest tests/ -v                                  # run all tests
uv run pytest tests/ --cov=. --cov-report=term-missing   # with coverage report
```

**Always run tests after making changes.** If tests fail, fix before running the bot.

## State Persistence

The bot saves state to `data/state.json` on every trade and on shutdown (Ctrl+C). On restart, it resumes:
- Open positions (waiting for matches to end)
- Session P&L and win/loss record
- Already-evaluated events (won't re-bet on same match)

## Live Trading

1. Create a **dedicated Polymarket wallet** (never use your main wallet)
2. Fund it with USDC on Polygon
3. Add the private key to `config.yaml`:
   ```yaml
   polymarket_private_key: "your_hex_private_key_without_0x"
   polymarket_funder: "0xYourWalletAddress"
   mode: "live"
   ```
4. Run `./run.sh --live` — it will ask for confirmation before starting

The bot auto-redeems winning positions to free up capital.

## Analyzing Results

```bash
uv run analyze.py --paper              # all paper trades
uv run analyze.py --today              # today only
uv run analyze.py --date 2026-03-21   # specific day
uv run analyze.py --live               # live trades
uv run analyze.py --all                # paper + live side by side
uv run analyze.py --days               # list trade files with quick stats
uv run analyze.py --wipe-paper         # delete all paper data
```

## Disclaimer

This bot is for educational and paper trading purposes. Polymarket's Terms of Service may restrict automated trading and/or access from certain jurisdictions. Use at your own risk. Past simulated performance does not guarantee future results.
