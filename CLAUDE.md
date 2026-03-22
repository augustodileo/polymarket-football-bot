# CLAUDE.md — Polymarket Football Bot

## What This Is

Automated football betting bot for Polymarket prediction markets. Monitors live football matches across 10+ leagues and places bets based on deterministic rules — no LLM in the trading loop. Runs in paper (simulated), live (real money), or ephemeral (throwaway) mode.

## Project Structure

```
src/
  main.py          — Orchestrator: discovery, monitoring, scheduling, execution, P&L tracking
  engine.py        — Decision engine: probability model (score + clock + Polymarket prices)
  analyze.py       — Trade performance analysis and reporting CLI
tests/             — Python unit tests (163 tests, pytest)
dashboard/
  index.html       — Static GitHub Pages frontend (Chart.js, vanilla JS)
  push.py          — Sidecar container: reads trade data, pushes JSON to gh-pages
  Dockerfile       — Dashboard sidecar image
  tests/
    dashboard.spec.js     — Playwright browser tests (22 tests, Chromium + WebKit)
    playwright.config.js  — Playwright configuration
.github/
  workflows/
    ci.yml         — Every push/PR + reusable: Python tests + Docker + container-structure-test + Playwright
    release.yml    — On v* tag: CI → GitHub Release → Deploy + smoke test (single workflow)
    deploy.yml     — Manual deploy + smoke test (workflow_dispatch, for hotfixes)
    pages.yml      — Deploy dashboard HTML to gh-pages (preserves dashboard-data.json)
  deploy.sh            — Shared deploy script (DRY, used by release + manual deploy)
config.example.yaml    — Template config (committed, no secrets)
config.yaml            — Real config (gitignored, mounted at runtime)
Dockerfile             — Bot container image
docker-compose.yml     — Runs bot + dashboard sidecar with shared data volume
container-structure-test.yaml — Google container-structure-test config
run.sh                 — Local launcher (--paper / --live / --ephemeral)
```

## Testing — MANDATORY

**Run tests after EVERY code change. Do not push if tests fail.**

### Python tests
```bash
uv run pytest tests/ -v                                   # all tests
uv run pytest tests/ --cov=src --cov-report=term-missing  # with coverage
```
CI enforces 80% minimum coverage. Branch protection requires test + docker jobs to pass.

### Playwright dashboard tests
```bash
cd dashboard/tests
npm install @playwright/test
npx playwright install --with-deps chromium webkit
npx playwright test --reporter=list
```
Tests use a mock `dashboard-data.json` with known values. A Python HTTP server serves the dashboard locally during tests.

### Container structure tests
```bash
docker build --build-arg VERSION=test -t poly-bot:test .
container-structure-test test --image poly-bot:test --config container-structure-test.yaml
```
Validates: files exist in image, secrets NOT baked in, Python/uv work, bot help runs, modules importable, env vars set, correct entrypoint.

### What's tested where

| Layer | Tool | File | Tests | What |
|-------|------|------|-------|------|
| Engine | pytest | test_engine.py | 38 | Probability model, market matching, Kelly sizing |
| Main logic | pytest | test_main.py | 35 | Parsing, team matching, mismatch detection, outcome, state |
| Orchestration | pytest | test_orchestration.py | 34 | Discovery, resolution, liquidity, redemption, display |
| Edge cases | pytest | test_coverage.py | 24 | Error paths, pre-match scanner, global scan, partial fills |
| Analysis | pytest | test_analyze.py | 21 | Trade loading, date filters, summaries, wipe |
| Run loop | pytest | test_run_loop.py | 11 | Pre-match execution, daily scan, full resolution flow |
| Docker image | container-structure-test | container-structure-test.yaml | 23 | Files, env vars, entrypoint, no secrets |
| Dashboard UI | Playwright | dashboard.spec.js | 22 | Cards, charts, filters, mobile, no JS errors |
| **Total** | | | **208** | |

## Versioning

**Git tags are the single source of truth.** No version in any source file.

```bash
git tag v1.5.0 && git push --tags
```

A `VERSION` file is generated at build/run time from `git describe --tags`. The startup banner reads it. Docker images are tagged with the version.

## CI/CD Pipeline

4 workflows total, no duplication. Smoke test is inlined into deploy steps (not a separate workflow).

| Event | Workflow | Steps |
|-------|----------|-------|
| Every push/PR | ci.yml | Python tests → coverage → Docker build + structure test → Playwright dashboard tests |
| Push `v*` tag | release.yml | Calls ci.yml → `gh release create` → deploy.sh + smoke test |
| Manual trigger | deploy.yml | deploy.sh + smoke test (for hotfixes) |
| Change `dashboard/index.html` | pages.yml | Checkout gh-pages → update only index.html → push (preserves data) |

### CI/CD gotchas

- **Node.js 20 deprecation**: All workflows use `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true` and `actions/checkout@v5`, `astral-sh/setup-uv@v7`. Do NOT use v4 of these actions — they'll break after June 2026.
- **`softprops/action-gh-release`**: Does NOT have a v3. We replaced it with `gh release create` CLI to avoid Node.js 20 dependency entirely.
- **GITHUB_TOKEN limitation**: Actions performed by `GITHUB_TOKEN` cannot trigger other workflows. That's why Release & Deploy is a single workflow (not separate release → deploy chain).
- **gh-pages deploy**: The pages workflow checks out the existing gh-pages branch and only updates `index.html`. It does NOT overwrite `dashboard-data.json` (the sidecar's data). This was a hard-won lesson — orphan branch force-push destroyed data multiple times.
- **Deploy script**: Encoded as base64 and sent via `envs` to `appleboy/ssh-action` because `script_file` param doesn't work (sends empty string).

## Architecture Decisions

### Two containers, one shared volume
```
poly-bot  (trading) → writes data/ → poly-dash (reads data/, pushes to GitHub)
GitHub Pages ← reads dashboard-data.json ← renders charts
```
The trading bot should NOT care about dashboards. If the GitHub API call fails, trading continues unaffected. Separation of concerns via sidecar pattern.

### No external API keys required
All data comes from Polymarket's free Gamma API + CLOB. We tried API-Football (free tier only covers 2022-2024 seasons) and The Odds API (rate limited at 500 req/month, burned through in one session). Both were removed. The probability model uses score + clock + Polymarket's own prices.

### Probability model (engine.py)
Simple math, not a trained model:
- Goal rate: ~0.033 per minute in late game
- P(goal in N minutes) = 1 - e^(-0.033 × N)
- Team strength inferred from Polymarket moneyline prices
- No base rate database, no clamping to bookmaker odds
- Compare estimate directly to Polymarket price for edge

This was iterated from: historical base rates (made up) → base rates + bookmaker clamping (circular when using Polymarket as bookmaker) → pure score + clock model.

### Tier mismatch = pre-match only
Once a match starts, Polymarket's live data is too slow/unreliable to verify clean conditions (no goals, no red cards). We can't distinguish "75% favorite at 0-0" from "75% because they scored." Pre-match prices are the only reliable mismatch signal.

Filter: `favorite >= 70% AND underdog <= 15%`. Both conditions required — a 66% favorite with a 20% underdog is not a mismatch, it's just a normal home advantage.

### Team name matching from market question text
Polymarket lists markets in arbitrary order. `_get_poly_implied()` matches team names from the question string (e.g., "Will Nashville SC win?") not from position in the array. This was a critical bug — the bot bet against Nashville (the favorite) because it assumed the first non-draw market was the home team.

### Liquidity: midpoint as primary signal
Polymarket's neg-risk orderbooks are misleading — `get_order_book` shows native orders only. Cross-book liquidity from complement tokens exists but is complex to calculate reliably. The `get_midpoint` API is the authoritative signal — if midpoint exists, the market is active.

Partial fills accepted: 50%+ of desired shares OR $500+ depth. 10 retries at 1-second intervals before skipping. Stake reduced to match available liquidity.

### State: only open positions + cumulative P&L persist
Evaluated events, scheduled bets, and caches reset each run. This prevents stale data from causing the bot to skip matches it should re-evaluate after a restart.

State saved via atomic write (temp file + rename) every poll cycle to prevent corruption.

### Paper and live data completely separate
`data/paper/` and `data/live/` are independent directories. Paper data can be wiped without affecting live. Ephemeral mode uses `data/ephemeral/` which is wiped on every start.

### Config injected at runtime
`config.yaml` is gitignored and NOT baked into the Docker image. In CI/CD, it's generated from `config.example.yaml` + GitHub Secrets/Variables via sed. On local, you copy `config.example.yaml` to `config.yaml`.

Non-sensitive config (BOT_MODE, BANKROLL) → GitHub Variables (plaintext, visible).
Secrets (POLYMARKET_PRIVATE_KEY, DEPLOY_SSH_KEY) → GitHub Secrets (encrypted, masked).

### Dashboard: static HTML + sidecar pusher
The dashboard is a single `index.html` with Chart.js. No framework, no build step, no server. The sidecar (`push.py`) reads `state.json` + `trades_*.jsonl` every 5 minutes and pushes `dashboard-data.json` to the gh-pages branch via GitHub API.

Chart.js gotchas:
- Do NOT use `chartjs-adapter-date-fns` — it fails to load from CDN and crashes all charts silently. Use category scale with pre-formatted time labels instead.
- Use `function()` syntax (not arrow functions) in Chart.js config callbacks for Safari compatibility.
- Destroy charts before re-creating on data refresh to prevent memory leaks.
- The PnL chart uses `stepped: 'before'` for a step-function look (PnL stays flat between trades).

### Daily loss limit
Configurable via `max_daily_loss` in config. Set to `0` to disable. When enabled, the bot stops taking new trades once cumulative session PnL hits the negative threshold.

### Print vs log
ALL output uses `log.info()` (stderr), NOT `print()` (stdout). In Docker, stdout is buffered and interleaves badly with stderr. This was a bug that caused the portfolio block to appear multiple times in logs.

Exception: the startup banner uses `sys.stdout.write()` + `flush()` because it's intentionally visual, not a log entry.

## Infrastructure

### GCP VM
- `e2-micro` in `us-east1-b` (free tier)
- Docker + docker-compose installed via startup script
- SSH access via deploy key (stored in GitHub Secrets)
- `--restart=always` on both containers

### GitHub Pages
- Source: `gh-pages` branch
- URL: `https://augustodileo.github.io/polymarket-football-bot/`
- Updated by sidecar every 5 minutes via GitHub API
- HTML updated by pages.yml workflow (only on `dashboard/index.html` changes)

## How to Run

```bash
# Local
./run.sh --paper
./run.sh --live
./run.sh --ephemeral

# Docker
docker-compose up -d

# Docker (manual)
docker build --build-arg VERSION=$(git describe --tags) -t poly-bot .
docker run -v $(pwd)/config.yaml:/app/config.yaml -v $(pwd)/data:/app/data poly-bot --paper

# Analyze
uv run src/analyze.py
uv run src/analyze.py --today
uv run src/analyze.py --days

# Release
git tag v1.5.0 && git push --tags

# Manual deploy
gh workflow run "Deploy (manual)"

# Check GCP bot
gcloud compute ssh poly-bot --zone=us-east1-b --command="sudo docker logs --tail 30 poly-bot"
```

## Common Tasks

- **Add a league**: Add to `leagues:` in config with `polymarket_tag` and `name`. The global scan (tag 100350) also catches unlisted leagues.
- **Tune strategy**: Edit `risk:` and `tier_mismatch:` in config.
- **Reset paper**: `uv run src/analyze.py --wipe-paper` or delete `data/paper/`
- **Update dashboard HTML**: Edit `dashboard/index.html`, push to main. Pages workflow auto-deploys.
- **Add new action version**: Check for Node.js 24 compatible versions. Use `@v5`+ for checkout, `@v7`+ for setup-uv. Avoid any action that only supports Node.js 20.
