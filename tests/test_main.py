"""Tests for main.py — parsing, mismatch detection, state, outcome resolution."""

import json
import pytest
from unittest.mock import MagicMock
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from main import (
    parse_score, parse_teams_from_title, _get_poly_implied,
    _is_tier_mismatch_from_poly, _determine_outcome,
    markets_to_dicts, save_state, load_state,
    _open_positions, _session_pnl, _session_wins, _session_losses, _session_trades,
)
import main as main_module


# ── Parse functions ─────────────────────────────────────────


class TestParseScore:
    def test_normal_score(self):
        assert parse_score("2-1") == (2, 1)

    def test_zero_zero(self):
        assert parse_score("0-0") == (0, 0)

    def test_high_score(self):
        assert parse_score("5-3") == (5, 3)

    def test_none(self):
        assert parse_score(None) == (0, 0)

    def test_empty_string(self):
        assert parse_score("") == (0, 0)

    def test_spaces(self):
        assert parse_score(" 2 - 1 ") == (2, 1)

    def test_garbage(self):
        assert parse_score("abc") == (0, 0)


class TestParseTeamsFromTitle:
    def test_vs_dot(self):
        h, a = parse_teams_from_title("Arsenal FC vs. Chelsea FC")
        assert h == "Arsenal FC"
        assert a == "Chelsea FC"

    def test_vs_no_dot(self):
        h, a = parse_teams_from_title("Arsenal FC vs Chelsea FC")
        assert h == "Arsenal FC"
        assert a == "Chelsea FC"

    def test_league_prefix(self):
        h, a = parse_teams_from_title("EPL: Arsenal FC vs. Chelsea FC")
        assert h == "Arsenal FC"
        assert a == "Chelsea FC"

    def test_serie_a_prefix(self):
        h, a = parse_teams_from_title("Serie A: AC Milan vs. Torino FC")
        assert h == "AC Milan"
        assert a == "Torino FC"

    def test_no_separator(self):
        h, a = parse_teams_from_title("Some Random Title")
        assert h == "Some Random Title"
        assert a == ""


# ── _get_poly_implied ───────────────────────────────────────


class TestGetPolyImplied:
    def test_correct_team_assignment(self):
        markets = [
            {"question": "Will Nashville SC win?", "outcome_prices": [0.66, 0.34]},
            {"question": "Will draw?", "outcome_prices": [0.20, 0.80]},
            {"question": "Will Orlando City SC win?", "outcome_prices": [0.14, 0.86]},
        ]
        h, d, a = _get_poly_implied(markets, "Nashville SC", "Orlando City SC")
        assert h == pytest.approx(0.66, abs=0.01)
        assert d == pytest.approx(0.20, abs=0.01)
        assert a == pytest.approx(0.14, abs=0.01)

    def test_reversed_market_order(self):
        markets = [
            {"question": "Will draw?", "outcome_prices": [0.20, 0.80]},
            {"question": "Will Orlando City SC win?", "outcome_prices": [0.14, 0.86]},
            {"question": "Will Nashville SC win?", "outcome_prices": [0.66, 0.34]},
        ]
        h, d, a = _get_poly_implied(markets, "Nashville SC", "Orlando City SC")
        assert h == pytest.approx(0.66, abs=0.01)
        assert a == pytest.approx(0.14, abs=0.01)

    def test_no_team_names_fallback(self):
        markets = [
            {"question": "Will Team A win?", "outcome_prices": [0.50, 0.50]},
            {"question": "Will draw?", "outcome_prices": [0.20, 0.80]},
            {"question": "Will Team B win?", "outcome_prices": [0.30, 0.70]},
        ]
        h, d, a = _get_poly_implied(markets)
        assert h > 0
        assert d > 0
        assert a > 0

    def test_empty_markets(self):
        h, d, a = _get_poly_implied([])
        assert h == 0 and d == 0 and a == 0

    def test_missing_prices(self):
        markets = [{"question": "Will X win?", "outcome_prices": []}]
        h, d, a = _get_poly_implied(markets, "X", "Y")
        assert h == 0


# ── _is_tier_mismatch_from_poly ─────────────────────────────


class TestIsTierMismatch:
    CONFIG = {"enabled": True, "min_favorite_prob": 0.65}

    def test_big_favorite(self):
        markets = [
            {"question": "Will Bayern win?", "outcome_prices": [0.85, 0.15]},
            {"question": "Will draw?", "outcome_prices": [0.10, 0.90]},
            {"question": "Will Augsburg win?", "outcome_prices": [0.05, 0.95]},
        ]
        is_mm, fav, und = _is_tier_mismatch_from_poly(markets, self.CONFIG)
        assert is_mm is True
        assert fav == pytest.approx(0.85, abs=0.01)

    def test_even_match_not_mismatch(self):
        markets = [
            {"question": "Will A win?", "outcome_prices": [0.40, 0.60]},
            {"question": "Will draw?", "outcome_prices": [0.25, 0.75]},
            {"question": "Will B win?", "outcome_prices": [0.35, 0.65]},
        ]
        is_mm, _, _ = _is_tier_mismatch_from_poly(markets, self.CONFIG)
        assert is_mm is False

    def test_settled_match_not_mismatch(self):
        markets = [
            {"question": "Will A win?", "outcome_prices": [1.0, 0.0]},
            {"question": "Will draw?", "outcome_prices": [0.0, 1.0]},
            {"question": "Will B win?", "outcome_prices": [0.0, 1.0]},
        ]
        is_mm, _, _ = _is_tier_mismatch_from_poly(markets, self.CONFIG)
        assert is_mm is False

    def test_near_settled_not_mismatch(self):
        markets = [
            {"question": "Will A win?", "outcome_prices": [0.96, 0.04]},
            {"question": "Will draw?", "outcome_prices": [0.03, 0.97]},
            {"question": "Will B win?", "outcome_prices": [0.01, 0.99]},
        ]
        is_mm, _, _ = _is_tier_mismatch_from_poly(markets, self.CONFIG)
        assert is_mm is False

    def test_disabled(self):
        markets = [
            {"question": "Will A win?", "outcome_prices": [0.90, 0.10]},
            {"question": "Will draw?", "outcome_prices": [0.05, 0.95]},
            {"question": "Will B win?", "outcome_prices": [0.05, 0.95]},
        ]
        is_mm, _, _ = _is_tier_mismatch_from_poly(markets, {"enabled": False})
        assert is_mm is False


# ── _determine_outcome ──────────────────────────────────────


class TestDetermineOutcome:
    def test_home_win_yes_side(self):
        pos = {"market_question": "Will Arsenal win?", "side": "YES",
               "home_team": "arsenal", "away_team": "chelsea"}
        assert _determine_outcome(pos, 2, 1) == "WIN"

    def test_home_win_no_side(self):
        pos = {"market_question": "Will Arsenal win?", "side": "NO",
               "home_team": "arsenal", "away_team": "chelsea"}
        assert _determine_outcome(pos, 2, 1) == "LOSS"

    def test_away_no_win_correct(self):
        pos = {"market_question": "Will Chelsea win?", "side": "NO",
               "home_team": "arsenal", "away_team": "chelsea"}
        assert _determine_outcome(pos, 2, 1) == "WIN"

    def test_draw_market_yes(self):
        pos = {"market_question": "Will the match end in a draw?", "side": "YES",
               "home_team": "arsenal", "away_team": "chelsea"}
        assert _determine_outcome(pos, 1, 1) == "WIN"

    def test_draw_market_no(self):
        pos = {"market_question": "Will the match end in a draw?", "side": "NO",
               "home_team": "arsenal", "away_team": "chelsea"}
        assert _determine_outcome(pos, 1, 1) == "LOSS"

    def test_draw_result_no_on_away(self):
        pos = {"market_question": "Will Chelsea win?", "side": "NO",
               "home_team": "arsenal", "away_team": "chelsea"}
        assert _determine_outcome(pos, 0, 0) == "WIN"

    def test_away_win_yes_side(self):
        pos = {"market_question": "Will Chelsea win?", "side": "YES",
               "home_team": "arsenal", "away_team": "chelsea"}
        assert _determine_outcome(pos, 0, 2) == "WIN"

    def test_away_win_no_side_loss(self):
        pos = {"market_question": "Will Chelsea win?", "side": "NO",
               "home_team": "arsenal", "away_team": "chelsea"}
        assert _determine_outcome(pos, 0, 2) == "LOSS"


# ── State persistence ──────────────────────────────────────


class TestStatePersistence:
    def test_save_and_load(self, tmp_path):
        main_module.TRADES_DIR = tmp_path
        main_module.STATE_FILE = tmp_path / "state.json"

        main_module._open_positions = {
            123: {"event_title": "Test", "stake": 500, "side": "NO",
                  "market_question": "Will X win?", "poly_price": 0.85,
                  "shares": 588, "profit_if_win": 88.2, "loss_if_lose": 499.8,
                  "edge_pct": 3.0, "token_id": "t", "score_at_entry": "0-0",
                  "minute": 80, "home_team": "A", "away_team": "B",
                  "opened_at": "2026-03-21T20:00:00Z", "true_prob": 0.88,
                  "book_implied": 0.0, "expected_value": 20.0,
                  "condition_id": "", "neg_risk": True}
        }
        main_module._session_pnl = 126.09
        main_module._session_wins = 1
        main_module._session_losses = 0
        main_module._session_trades = 1

        save_state()
        assert main_module.STATE_FILE.exists()

        main_module._open_positions = {}
        main_module._session_pnl = 0
        main_module._session_wins = 0

        load_state()
        assert 123 in main_module._open_positions
        assert main_module._open_positions[123]["stake"] == 500
        assert main_module._session_pnl == 126.09
        assert main_module._session_wins == 1

    def test_load_missing_file(self, tmp_path):
        main_module.STATE_FILE = tmp_path / "nonexistent.json"
        main_module._open_positions = {}
        main_module._session_pnl = 0
        load_state()
        assert main_module._open_positions == {}


# ── markets_to_dicts ────────────────────────────────────────


class TestMarketsToDicts:
    def test_filters_moneyline_only(self):
        m1 = MagicMock()
        m1.sports_market_type = "moneyline"
        m1.question = "Will A win?"
        m1.outcome_prices = "[0.5, 0.5]"
        m1.token_ids = '["t1", "t2"]'
        m1.outcomes = '["Yes", "No"]'
        m1.accepting_orders = True
        m1.condition_id = "cond1"
        m1.neg_risk = True

        m2 = MagicMock()
        m2.sports_market_type = "totals"

        result = markets_to_dicts([m1, m2])
        assert len(result) == 1
        assert result[0]["question"] == "Will A win?"

    def test_parses_json_strings(self):
        m = MagicMock()
        m.sports_market_type = "moneyline"
        m.question = "Will A win?"
        m.outcome_prices = '[0.6, 0.4]'
        m.token_ids = '["tok1", "tok2"]'
        m.outcomes = '["Yes", "No"]'
        m.accepting_orders = True
        m.condition_id = "c1"
        m.neg_risk = False

        result = markets_to_dicts([m])
        assert result[0]["outcome_prices"] == [0.6, 0.4]
        assert result[0]["token_ids"] == ["tok1", "tok2"]

    def test_empty_list(self):
        assert markets_to_dicts([]) == []
