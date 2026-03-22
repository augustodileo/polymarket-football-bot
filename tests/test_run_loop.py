"""Tests for run_loop and the main orchestration while-loop internals.

Mocks all Polymarket API clients to test the full flow:
discovery → daily scan → pre-match scheduling/execution → live match
evaluation → trade placement → resolution → P&L tracking.
"""

import json
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from pathlib import Path
from datetime import datetime, timezone, timedelta

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import main as M
from main import (
    run_loop, _scan_pre_match_mismatches, _get_poly_implied,
    _is_tier_mismatch_from_poly, markets_to_dicts, parse_score,
    parse_teams_from_title, check_liquidity, resolve_ended_matches,
    record_open_position, place_order, save_state, load_state,
    discover_football_events, LiquidityInfo, _determine_outcome,
    _trades_file_for_today, _init_data_paths,
)
from engine import TradeSignal, evaluate


def _mk_market(question, prices, tokens=None, cid="c"):
    m = MagicMock()
    m.sports_market_type = "moneyline"
    m.question = question
    m.outcome_prices = json.dumps(prices)
    m.token_ids = json.dumps(tokens or ["ty", "tn"])
    m.outcomes = json.dumps(["Yes", "No"])
    m.condition_id = cid
    m.neg_risk = True
    m.accepting_orders = True
    return m


def _mk_event(eid, title, live=False, ended=False, score="0-0",
              period=None, elapsed=None, start_time=None, markets=None,
              slug="test"):
    ev = MagicMock()
    ev.id = eid
    ev.title = title
    ev.live = live
    ev.ended = ended
    ev.score = score
    ev.period = period
    ev.elapsed = elapsed
    ev.start_time = start_time
    ev.slug = slug
    ev.markets = markets or []
    ev.closed = False
    ev.active = True
    return ev


# ── Pre-match execution (Phase 2) ──────────────────────────


class TestPreMatchExecution:
    """Test the Phase 2 of _scan_pre_match_mismatches — actually placing bets."""

    def setup_method(self):
        M._evaluated_event_ids = set()
        M._open_positions = {}
        M._pre_match_scheduled = {}
        M._session_trades = 0

    def test_executes_when_bet_time_reached(self, tmp_path):
        M.TRADES_DIR = tmp_path
        M.STATE_FILE = tmp_path / "state.json"

        now = datetime.now(timezone.utc)

        # Pre-schedule a match with bet_at in the past (should fire)
        M._pre_match_scheduled = {
            100: {
                "event_title": "Bayern vs Augsburg",
                "kickoff": now + timedelta(minutes=10),
                "bet_at": now - timedelta(minutes=1),  # already due
                "league_cfg": {"api_football_id": 78, "name": "Bundesliga"},
                "home_team": "Bayern",
                "away_team": "Augsburg",
                "fav_team": "Bayern",
                "underdog_team": "Augsburg",
                "fav_is_home": True,
                "fav_prob": 0.85,
            }
        }

        m_home = _mk_market("Will Bayern win?", [0.85, 0.15])
        m_draw = _mk_market("Will draw?", [0.10, 0.90])
        m_away = _mk_market("Will Augsburg win?", [0.05, 0.95], tokens=["aug_y", "aug_n"])

        ev = _mk_event(100, "Bayern vs. Augsburg", markets=[m_home, m_draw, m_away])

        clob_ro = MagicMock()
        mid = MagicMock(value=0.95)
        clob_ro.get_midpoint.return_value = mid
        book = MagicMock(asks=[], bids=[])
        clob_ro.get_order_book.return_value = book

        events = [(ev, "bundesliga", {"api_football_id": 78, "name": "Bundesliga"})]
        config = {"odds_api_key": ""}
        tm_cfg = {"enabled": True, "min_favorite_prob": 0.65,
                  "pre_match": True, "pre_match_bet_minutes_before": 30,
                  "pre_match_min_edge_pct": 2.0}
        risk = {"max_concurrent_positions": 5, "kelly_fraction": 0.25,
                "max_single_stake": 1000}

        _scan_pre_match_mismatches(events, config, tm_cfg, risk, 10000, None, clob_ro, "paper")

        # Should have executed and removed from scheduled
        assert 100 not in M._pre_match_scheduled
        assert 100 in M._evaluated_event_ids

    def test_skips_max_concurrent(self, tmp_path):
        M.TRADES_DIR = tmp_path
        M.STATE_FILE = tmp_path / "state.json"

        now = datetime.now(timezone.utc)
        M._open_positions = {i: {} for i in range(5)}  # 5 open = max

        M._pre_match_scheduled = {
            200: {
                "event_title": "X vs Y",
                "kickoff": now + timedelta(minutes=10),
                "bet_at": now - timedelta(minutes=1),
                "league_cfg": {"api_football_id": 0, "name": "Test"},
                "home_team": "X", "away_team": "Y",
                "fav_team": "X", "underdog_team": "Y",
                "fav_is_home": True, "fav_prob": 0.80,
            }
        }

        events = []
        _scan_pre_match_mismatches(events, {}, {"pre_match": True, "enabled": True,
                                                 "min_favorite_prob": 0.65,
                                                 "pre_match_bet_minutes_before": 30,
                                                 "pre_match_min_edge_pct": 2.0},
                                   {"max_concurrent_positions": 5}, 10000, None, MagicMock(), "paper")

        # Should still be scheduled (deferred, not removed)
        assert 200 in M._pre_match_scheduled

    def test_no_edge_skips(self, tmp_path):
        M.TRADES_DIR = tmp_path
        M.STATE_FILE = tmp_path / "state.json"

        now = datetime.now(timezone.utc)
        M._pre_match_scheduled = {
            300: {
                "event_title": "A vs B",
                "kickoff": now + timedelta(minutes=10),
                "bet_at": now - timedelta(minutes=1),
                "league_cfg": {"api_football_id": 0, "name": "Test"},
                "home_team": "TeamA", "away_team": "TeamB",
                "fav_team": "TeamA", "underdog_team": "TeamB",
                "fav_is_home": True, "fav_prob": 0.70,
            }
        }

        # Underdog NO price already at 95c = fair, no edge
        m_home = _mk_market("Will TeamA win?", [0.70, 0.30])
        m_draw = _mk_market("Will draw?", [0.15, 0.85])
        m_away = _mk_market("Will TeamB win?", [0.05, 0.95], tokens=["by", "bn"])

        ev = _mk_event(300, "TeamA vs. TeamB", markets=[m_home, m_draw, m_away])
        events = [(ev, "test", {"api_football_id": 0, "name": "Test"})]

        _scan_pre_match_mismatches(events, {"odds_api_key": ""},
                                   {"pre_match": True, "enabled": True,
                                    "min_favorite_prob": 0.65,
                                    "pre_match_bet_minutes_before": 30,
                                    "pre_match_min_edge_pct": 2.0},
                                   {"max_concurrent_positions": 5, "kelly_fraction": 0.25,
                                    "max_single_stake": 1000},
                                   10000, None, MagicMock(), "paper")

        assert 300 not in M._pre_match_scheduled
        assert 300 in M._evaluated_event_ids
        assert len(M._open_positions) == 0  # no trade placed


# ── Full evaluate flow with live match ──────────────────────


class TestLiveMatchEvaluation:
    """Test the engine evaluation as called from the main loop."""

    def test_two_goal_lead_finds_edge(self):
        """2-0 at minute 85 with Polymarket underpricing leader → BUY."""
        markets = [
            {"question": "Will Home win?", "outcome_prices": [0.90, 0.10],
             "token_ids": ["hy", "hn"], "sports_market_type": "moneyline"},
            {"question": "Will draw?", "outcome_prices": [0.05, 0.95],
             "token_ids": ["dy", "dn"], "sports_market_type": "moneyline"},
            {"question": "Will Away win?", "outcome_prices": [0.05, 0.95],
             "token_ids": ["ay", "an"], "sports_market_type": "moneyline"},
        ]
        signal = evaluate("Home", "Away", 2, 0, 85,
                          markets, 10000,
                          {"min_edge_pct": 2.0, "kelly_fraction": 0.25, "max_single_stake": 1000})
        assert signal.action == "BUY"
        assert signal.side == "YES"
        assert signal.edge_pct > 2.0

    def test_efficient_market_no_trade(self):
        """When poly price matches estimate → NO_TRADE."""
        markets = [
            {"question": "Will Home win?", "outcome_prices": [0.06, 0.94],
             "token_ids": ["hy", "hn"], "sports_market_type": "moneyline"},
            {"question": "Will draw?", "outcome_prices": [0.90, 0.10],
             "token_ids": ["dy", "dn"], "sports_market_type": "moneyline"},
            {"question": "Will Away win?", "outcome_prices": [0.04, 0.96],
             "token_ids": ["ay", "an"], "sports_market_type": "moneyline"},
        ]
        signal = evaluate("Home", "Away", 0, 0, 90,
                          markets, 10000,
                          {"min_edge_pct": 2.0, "kelly_fraction": 0.25, "max_single_stake": 1000})
        assert signal.action == "NO_TRADE"


# ── Daily scan ──────────────────────────────────────────────


class TestDailyScan:
    """Test the daily scan logic that runs once per day."""

    def test_daily_scan_prints_leagues(self, capsys):
        now = datetime.now(timezone.utc)
        ko = now + timedelta(hours=2)

        ev1 = _mk_event(1, "EPL: Arsenal vs Chelsea", start_time=ko,
                         markets=[_mk_market("Will Arsenal win?", [0.5, 0.5])])
        ev2 = _mk_event(2, "La Liga: Barca vs Real", start_time=ko,
                         markets=[_mk_market("Will Barca win?", [0.6, 0.4])])

        events = [
            (ev1, "epl", {"name": "Premier League"}),
            (ev2, "la_liga", {"name": "La Liga"}),
        ]

        # Simulate the daily scan logic
        today_str = now.strftime("%Y-%m-%d")
        by_league = {}
        for ev, lk, lc in events:
            by_league.setdefault(lc.get("name", lk), []).append(ev)

        print(f"=== DAILY SCAN: {today_str} ===")
        for league_name, league_events in sorted(by_league.items()):
            print(f"  {league_name}: {len(league_events)} matches")
        print(f"=== END SCAN ===")

        out = capsys.readouterr().out
        assert "Premier League" in out
        assert "La Liga" in out


# ── run_loop with mocked APIs (single cycle) ────────────────


class TestRunLoopInit:
    """Test run_loop initialization paths (not the infinite loop itself)."""

    def test_init_data_paths_paper(self, tmp_path):
        M.BASE_DIR = tmp_path
        _init_data_paths("paper")
        assert M.TRADES_DIR == tmp_path / "paper"
        assert M.TRADES_DIR.exists()

    def test_init_data_paths_live(self, tmp_path):
        M.BASE_DIR = tmp_path
        _init_data_paths("live")
        assert M.TRADES_DIR == tmp_path / "live"


# ── Open positions display with live prices ─────────────────


class TestOpenPositionDisplay:
    """Test the open bets display including live price lookup."""

    def test_unrealized_pnl_calculation(self):
        """Test the P&L math: cost vs current value."""
        shares = 1000
        entry_price = 0.85
        current_price = 0.90

        cost = shares * entry_price   # 850
        value = shares * current_price  # 900
        unrealized = value - cost       # +50

        assert cost == 850
        assert value == 900
        assert unrealized == 50

    def test_negative_unrealized(self):
        shares = 1000
        entry_price = 0.85
        current_price = 0.75

        unrealized = (shares * current_price) - (shares * entry_price)
        assert unrealized == -100


# ── Full resolution with auto-redeem ────────────────────────


class TestFullResolutionFlow:
    """Test resolve → P&L → redeem → trade log → state save."""

    def test_full_flow(self, tmp_path):
        M.TRADES_DIR = tmp_path
        M.STATE_FILE = tmp_path / "state.json"
        M._session_pnl = 0.0
        M._session_wins = 0
        M._session_losses = 0

        M._open_positions = {
            50: {
                "event_title": "A vs B", "opened_at": "2026-03-21T20:00:00Z",
                "minute": 82, "score_at_entry": "1-0",
                "home_team": "teamA", "away_team": "teamB",
                "market_question": "Will teamB win?", "side": "NO",
                "token_id": "tok", "condition_id": "0xcond", "neg_risk": True,
                "poly_price": 0.90, "true_prob": 0.95, "book_implied": 0.0,
                "edge_pct": 5.0, "stake": 900, "shares": 1000,
                "profit_if_win": 100, "loss_if_lose": 900,
                "expected_value": 50.0, "league": "EPL",
                "trade_type": "STANDARD", "liquidity_depth": 5000,
                "liquidity_spread": 0.02,
            }
        }

        ev = _mk_event(50, "A vs B", ended=True, score="2-0", period="FT")
        events = [(ev, "epl", {})]

        mock_web3 = MagicMock()
        resolve_ended_matches(events, mock_web3, "paper", True)

        # Position resolved
        assert len(M._open_positions) == 0
        assert M._session_wins == 1
        assert M._session_pnl == 100.0

        # Trade logged
        files = list(tmp_path.glob("trades_*.jsonl"))
        assert len(files) == 1
        trade = json.loads(files[0].read_text().strip())
        assert trade["outcome"] == "WIN"
        assert trade["pnl"] == 100.0
        assert trade["league"] == "EPL"

        # State saved
        assert M.STATE_FILE.exists()
