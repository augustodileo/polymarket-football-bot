# CLAUDE.md — Polymarket Football Bot

## What This Is

Automated football betting bot for Polymarket prediction markets. Monitors live football matches across 10+ leagues and places bets based on deterministic rules — no LLM in the trading loop. Runs in paper (simulated), live (real money), or ephemeral (throwaway) mode.

## Architecture

Single Python process in a Docker container. Deployed on GCP via GitHub Actions CI/CD.

```
src/
  main.py      — Orchestrator: discovery, monitoring, scheduling, execution, P&L tracking
  engine.py    — Decision engine: probability model (score + clock + Polymarket prices)
  stats.py     — Data types (MatchStats, BookmakerOdds)
  analyze.py   — Trade performance analysis and reporting
tests/         — 163 unit tests, 87% coverage
.github/
  workflows/
    ci.yml         — Every push: tests + coverage + Docker build
    release.yml    — On v* tag: test → release → deploy → smoke test
    deploy.yml     — Manual deploy (workflow_dispatch)
    smoke-test.yml — Post-deploy health check (reusable)
  deploy.sh        — Shared deploy script (DRY)
config.example.yaml — Template config (no secrets, committed)
config.yaml         — Real config (gitignored, mounted at runtime)
run.sh              — Local launcher (--paper / --live / --ephemeral)
Dockerfile          — Container image, version from git tag build arg
```

## Testing — MANDATORY

**Run tests after EVERY code change.** Do not push if tests fail.

```bash
uv run pytest tests/ -v                                   # run all
uv run pytest tests/ --cov=. --cov-report=term-missing    # with coverage
```

CI enforces 80% minimum coverage. Branch protection requires test + docker jobs to pass.

### Test files

| File | What | Tests |
|------|------|-------|
| `test_engine.py` | Probability model, market matching, evaluate, Kelly | 40 |
| `test_main.py` | Parsing, team matching, mismatch detection, state | 35 |
| `test_orchestration.py` | Discovery, resolution, liquidity, redemption | 36 |
| `test_analyze.py` | Trade loading, date filters, summaries, wipe | 21 |
| `test_coverage.py` | Edge cases, error paths, pre-match scanner | 19 |
| `test_run_loop.py` | Pre-match execution, daily scan, full flow | 12 |

### When adding new functionality

1. Write tests first or immediately after
2. Mock external APIs — never call real Polymarket in tests
3. Run full suite before pushing
4. CI will block merge if tests fail or coverage drops below 80%

## Versioning

**Git tags are the single source of truth.** No version in any file.

```bash
git tag v1.3.0
git push --tags
```

This triggers: CI → Release (with changelog) → Deploy to GCP → Smoke test.

A `VERSION` file is generated at build/run time from `git describe --tags`. The startup banner reads it.

## CI/CD Pipeline

| Event | Workflow | Steps |
|-------|----------|-------|
| Every push/PR | CI | Tests → coverage check → Docker build + smoke |
| Push `v*` tag | Release & Deploy | CI → `gh release create` → deploy.sh via SSH → smoke test |
| Manual trigger | Deploy | deploy.sh via SSH → smoke test |

Deploy script (`deploy.sh`): `git pull` → generate config from template + GitHub secrets/vars → `docker build` with version → `docker run`.

## Key Design Decisions

- **No external API keys required.** All data from Polymarket's free Gamma API + CLOB.
- **Probability model uses score + clock + Polymarket prices.** Simple math: goal rate per minute × time remaining → probability. Polymarket prices indicate team strength. No historical database.
- **Tier mismatch = pre-match only.** Bet NO on weak underdogs (< 15%) when facing strong favorites (> 70%), 30 min before kickoff. Once match starts → standard minute 80+ rules.
- **Team name matching from market question text.** `_get_poly_implied()` matches team names from the question string to handle arbitrary Polymarket market ordering.
- **Liquidity: midpoint as primary signal.** Neg-risk orderbooks are misleading — `get_midpoint` confirms market is active. Partial fills accepted (50%+ or $500+ depth). 10 retries at 1s intervals.
- **Paper = live minus one API call.** Identical P&L tracking, bankroll updates, result resolution. Only `place_order()` is skipped.
- **Separate data directories.** `data/paper/`, `data/live/`, `data/ephemeral/`. Paper never touches live.
- **State: only open positions + cumulative P&L persist.** Evaluated events, scheduled bets, caches reset each run.
- **Config injected at runtime.** `config.yaml` is gitignored. Docker gets it via volume mount. CI/CD generates it from `config.example.yaml` + GitHub Secrets/Variables.

## Config

Secrets (GitHub Secrets — encrypted):
- `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY` — GCP VM access
- `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER` — wallet (live mode only)

Variables (GitHub Variables — plaintext):
- `BOT_MODE` — paper/live/ephemeral
- `BANKROLL` — starting bankroll

## How to Run

```bash
# Local
./run.sh --paper
./run.sh --live
./run.sh --ephemeral

# Docker
docker run -v $(pwd)/config.yaml:/app/config.yaml -v $(pwd)/data:/app/data poly-bot --paper

# Analyze
uv run src/analyze.py
uv run src/analyze.py --today
uv run src/analyze.py --days
```

## Common Tasks

- **Release:** `git tag v1.3.0 && git push --tags`
- **Manual deploy:** GitHub Actions → Deploy (manual) → Run workflow
- **Add a league:** Add to `leagues:` in config with `polymarket_tag` and `name`
- **Tune strategy:** Edit `risk:` and `tier_mismatch:` in config
- **Check GCP bot:** `gcloud compute ssh poly-bot --zone=us-east1-b --command="sudo docker logs --tail 30 poly-bot"`
- **Reset paper:** Delete `data/paper/` or `uv run src/analyze.py --wipe-paper`
