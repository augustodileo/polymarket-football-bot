# CLAUDE.md — Polymarket Football Bot

## What This Is

Automated football betting bot for Polymarket prediction markets. Runs in paper mode (simulated) or live mode (real money). Monitors live football matches across 10+ leagues and places bets based on deterministic rules — no LLM in the trading loop.

## Architecture

Single Python process, no Docker, no microservices. Uses `uv` for dependency management with Python 3.12.

```
main.py        — Orchestrator: discovery, monitoring, scheduling, execution, P&L tracking
engine.py      — Decision engine: probability model (score + clock), edge calculation, Kelly sizing
stats.py       — Data types (MatchStats, BookmakerOdds) — no external API calls
analyze.py     — Trade performance analysis and reporting
config.yaml    — All settings: bankroll, risk limits, leagues, strategy params
run.sh         — Launcher script (requires --paper, --live, or --ephemeral flag)
data/
  paper/       — Paper mode state + daily trade logs (ephemeral, can be wiped)
  live/        — Live mode state + daily trade logs (real money, never wiped)
  ephemeral/   — Throwaway runs, wiped on every start
tests/         — Unit tests (134 tests, 80% coverage)
```

## Testing — MANDATORY

**Run tests after EVERY code change.** This is not optional.

```bash
uv run pytest tests/ -v                    # run all tests
uv run pytest tests/ --cov=. --cov-report=term-missing  # with coverage
```

If any test fails, fix it before committing or deploying. Do not skip failing tests.

### Test structure

- `tests/test_engine.py` — Probability model, market matching, full evaluate function, edge cases, Kelly sizing (31 tests, engine.py at 94%)
- `tests/test_main.py` — Parsing, team-to-price mapping, mismatch detection, outcome resolution, state persistence, market conversion (35 tests)
- `tests/test_orchestration.py` — Discovery, resolution, liquidity checks, order placement, redemption, print functions, trade logging (36 tests)
- `tests/test_analyze.py` — Trade loading, date filtering, summaries, wipe, day listing (14 tests)
- `tests/test_coverage.py` — Edge cases, error paths, global scan, cross-book liquidity, pre-match scanner, partial fills (18 tests)

### When adding new functionality

1. Write the test first (or immediately after)
2. Mock external APIs (Polymarket Gamma, CLOB) — never call real APIs in tests
3. Run full suite: `uv run pytest tests/ -v`
4. Check coverage didn't drop: `uv run pytest tests/ --cov=. --cov-report=term-missing`

### Known untested code

- `main.py run_loop()` (~400 lines) — the main while loop with real API calls. Tested via paper/ephemeral mode against live Polymarket data.
- Integration between all components across multiple poll cycles — tested manually.

## Key Design Decisions

- **No external API keys required.** All data comes from Polymarket's free Gamma API + CLOB. API-Football and Odds API were removed.
- **Probability model uses score + clock + Polymarket prices.** No historical base rate database. Simple math: given the score and minutes remaining, what should the probability be? Compare to Polymarket price for edge.
- **Tier mismatch = pre-match only.** Once a match starts, we can't verify clean conditions (no goals, no red cards) from Polymarket data alone, so live matches are standard rules (minute 80+).
- **Team name matching uses market question text.** Polymarket lists markets in arbitrary order, so `_get_poly_implied` matches team names from the question string, not position.
- **Neg-risk orderbook:** Polymarket uses neg-risk markets where YES+NO = $1. The `get_midpoint` API is the primary liquidity signal. Cross-book liquidity from complement tokens is checked as secondary.
- **Partial fills accepted.** If at least 50% of desired shares or $500+ depth is available, the bot trades at reduced size. 10 retries at 1-second intervals before skipping.
- **State persists across restarts** via `data/{mode}/state.json`. Only open positions + cumulative P&L persist. Evaluated events, scheduled bets, and caches reset each run.
- **Paper and live data are completely separate.** Paper mode writes to `data/paper/`, live to `data/live/`. Paper data can be wiped without affecting live.

## How to Run

```bash
./run.sh --paper        # paper trading, state persists
./run.sh --live         # real money (prompts for confirmation)
./run.sh --ephemeral    # throwaway run, wiped on start
```

## Dependencies

All managed by `uv`. Key packages:
- `polymarket-apis` — wraps Gamma API, CLOB, WebSocket, Web3
- `requests`, `pyyaml`, `websockets`
- `pytest`, `pytest-cov` — testing

## Common Tasks

- **Add a league:** Add entry to `leagues:` in config.yaml with `polymarket_tag` and `api_football_id`. The global football scan (tag 100350) also catches unlisted leagues automatically.
- **Tune strategy:** Edit `risk:` and `tier_mismatch:` sections in config.yaml. Key params: `min_edge_pct`, `kelly_fraction`, `max_single_stake`, `min_favorite_prob`.
- **Analyze results:** `uv run analyze.py --paper` or `uv run analyze.py --live`
- **Reset paper:** `uv run analyze.py --wipe-paper` or delete `data/paper/`
- **Run tests:** `uv run pytest tests/ -v` — do this after every change.
