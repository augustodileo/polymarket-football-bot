#!/usr/bin/env python3
"""
push.py — Dashboard data pusher (sidecar container).

Reads bot trade data from shared volume, builds a JSON summary,
and pushes it to the gh-pages branch via GitHub API.

Runs on a loop every PUSH_INTERVAL_SEC (default 300 = 5 minutes).
"""

import json
import os
import sys
import time
import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data/paper"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
PUSH_INTERVAL = int(os.environ.get("PUSH_INTERVAL_SEC", "300"))
DASHBOARD_FILE = "dashboard-data.json"


def load_all_trades() -> list[dict]:
    """Load all trades from daily JSONL files."""
    trades = []
    for f in sorted(DATA_DIR.glob("trades_*.jsonl")):
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    trades.append(json.loads(line))
    return trades


def load_state() -> dict:
    """Load current bot state."""
    state_file = DATA_DIR / "state.json"
    if state_file.exists():
        with open(state_file) as f:
            return json.load(f)
    return {}


def build_dashboard_data() -> dict:
    """Build the JSON payload for the dashboard."""
    trades = load_all_trades()
    state = load_state()

    # P&L curve
    pnl_curve = []
    running = 0.0
    for t in trades:
        running += t.get("pnl", 0)
        pnl_curve.append({
            "timestamp": t.get("resolved_at", t.get("timestamp", "")),
            "event": t.get("event", ""),
            "pnl": round(t.get("pnl", 0), 2),
            "cumulative": round(running, 2),
            "outcome": t.get("outcome", ""),
            "side": t.get("side", ""),
            "market": t.get("market", ""),
            "edge_pct": round(t.get("edge_pct", 0), 1),
            "stake": round(t.get("stake", 0), 2),
            "league": t.get("league", ""),
            "trade_type": t.get("trade_type", ""),
            "score_at_entry": t.get("score_at_entry", ""),
            "final_score": t.get("final_score", ""),
            "minute": t.get("minute", 0),
        })

    # Summary stats
    wins = sum(1 for t in trades if t.get("outcome") == "WIN")
    losses = sum(1 for t in trades if t.get("outcome") == "LOSS")
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    total_staked = sum(t.get("stake", 0) for t in trades)

    # By league
    by_league = {}
    for t in trades:
        lg = t.get("league") or "Unknown"
        by_league.setdefault(lg, {"trades": 0, "wins": 0, "pnl": 0})
        by_league[lg]["trades"] += 1
        if t.get("outcome") == "WIN":
            by_league[lg]["wins"] += 1
        by_league[lg]["pnl"] = round(by_league[lg]["pnl"] + t.get("pnl", 0), 2)

    # By day
    by_day = {}
    for t in trades:
        day = (t.get("resolved_at") or t.get("timestamp", ""))[:10]
        by_day.setdefault(day, {"trades": 0, "wins": 0, "pnl": 0})
        by_day[day]["trades"] += 1
        if t.get("outcome") == "WIN":
            by_day[day]["wins"] += 1
        by_day[day]["pnl"] = round(by_day[day]["pnl"] + t.get("pnl", 0), 2)

    # Open positions
    open_positions = []
    for eid, pos in state.get("open_positions", {}).items():
        open_positions.append({
            "event": pos.get("event_title", ""),
            "side": pos.get("side", ""),
            "market": pos.get("market_question", ""),
            "stake": pos.get("stake", 0),
            "entry_price": pos.get("poly_price", 0),
            "edge_pct": pos.get("edge_pct", 0),
            "score_at_entry": pos.get("score_at_entry", ""),
            "minute": pos.get("minute", 0),
            "profit_if_win": pos.get("profit_if_win", 0),
            "loss_if_lose": pos.get("loss_if_lose", 0),
        })

    # Scheduled pre-match bets
    scheduled_bets = []
    for eid, sched in state.get("scheduled_bets", {}).items():
        scheduled_bets.append({
            "event": sched.get("event_title", ""),
            "fav_team": sched.get("fav_team", ""),
            "underdog_team": sched.get("underdog_team", ""),
            "fav_prob": sched.get("fav_prob", 0),
            "kickoff": sched.get("kickoff", ""),
            "bet_at": sched.get("bet_at", ""),
        })

    return {
        "version": state.get("version", "dev"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(trades) * 100, 1) if trades else 0,
            "total_pnl": round(total_pnl, 2),
            "total_staked": round(total_staked, 2),
            "roi": round(total_pnl / total_staked * 100, 1) if total_staked > 0 else 0,
        },
        "open_positions": open_positions,
        "scheduled_bets": scheduled_bets,
        "todays_schedule": state.get("todays_schedule", []),
        "pnl_curve": pnl_curve,
        "by_league": by_league,
        "by_day": by_day,
    }


def push_to_github(data: dict):
    """Push dashboard-data.json to the gh-pages branch via GitHub API."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log.warning("GITHUB_TOKEN or GITHUB_REPO not set — skipping push")
        return

    content = json.dumps(data, indent=2)
    encoded = base64.b64encode(content.encode()).decode()

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{DASHBOARD_FILE}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }

    # Get current file SHA (needed for updates)
    sha = None
    try:
        req = Request(f"{api_url}?ref=gh-pages", headers=headers)
        resp = urlopen(req)
        sha = json.loads(resp.read()).get("sha")
    except URLError:
        pass  # file doesn't exist yet

    payload = {
        "message": f"Update dashboard data {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
        "content": encoded,
        "branch": "gh-pages",
    }
    if sha:
        payload["sha"] = sha

    try:
        req = Request(api_url, data=json.dumps(payload).encode(), headers=headers, method="PUT")
        resp = urlopen(req)
        log.info(f"Pushed dashboard data ({len(content)} bytes)")
    except URLError as e:
        log.error(f"Failed to push: {e}")


def main():
    log.info(f"Dashboard pusher started | Data: {DATA_DIR} | Interval: {PUSH_INTERVAL}s")
    log.info(f"Repo: {GITHUB_REPO or '(not set)'}")

    while True:
        try:
            data = build_dashboard_data()
            log.info(f"Built data: {data['summary']['total_trades']} trades, "
                     f"${data['summary']['total_pnl']:+.2f} P&L, "
                     f"{len(data['open_positions'])} open")
            push_to_github(data)
        except Exception as e:
            log.error(f"Error: {e}")

        time.sleep(PUSH_INTERVAL)


if __name__ == "__main__":
    main()
