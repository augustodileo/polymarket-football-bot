"""Tests for main.py orchestration — discovery, resolution, pre-match, liquidity."""

import json
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import main as M
from main import (
    discover_football_events, resolve_ended_matches, check_liquidity,
    _scan_pre_match_mismatches, _get_bankroll, _init_data_paths,
    record_open_position, place_order, redeem_winning_position,
    print_signal, print_status, LiquidityInfo,
    _get_poly_implied, _is_tier_mismatch_from_poly, _guess_league_cfg,
    _trades_file_for_today,
)
from engine import TradeSignal
from stats import BookmakerOdds, MatchStats


# ── Helpers ─────────────────────────────────────────────────


def _make_event(title="A vs B", live=False, ended=False, score="0-0",
                period=None, elapsed=None, start_time=None, markets=None,
                event_id=1, slug="test-slug"):
    ev = MagicMock()
    ev.id = event_id
    ev.title = title
    ev.live = live
    ev.ended = ended
    ev.score = score
    ev.period = period
    ev.elapsed = elapsed
    ev.start_time = start_time
    ev.slug = slug
    ev.markets = markets or []
    return ev


def _make_market(question="Will A win?", smt="moneyline", prices=None, tokens=None,
                 outcomes=None, condition_id="cond", neg_risk=True, accepting=True):
    m = MagicMock()
    m.sports_market_type = smt
    m.question = question
    m.outcome_prices = json.dumps(prices or [0.5, 0.5])
    m.token_ids = json.dumps(tokens or ["tok_yes", "tok_no"])
    m.outcomes = json.dumps(outcomes or ["Yes", "No"])
    m.condition_id = condition_id
    m.neg_risk = neg_risk
    m.accepting_orders = accepting
    return m


# ── _init_data_paths ────────────────────────────────────────


class TestInitDataPaths:
    def test_paper_path(self, tmp_path):
        M.BASE_DIR = tmp_path
        _init_data_paths("paper")
        assert M.TRADES_DIR == tmp_path / "paper"
        assert M.STATE_FILE == tmp_path / "paper" / "state.json"
        assert M.TRADES_DIR.exists()

    def test_live_path(self, tmp_path):
        M.BASE_DIR = tmp_path
        _init_data_paths("live")
        assert M.TRADES_DIR == tmp_path / "live"

    def test_ephemeral_path(self, tmp_path):
        M.BASE_DIR = tmp_path
        _init_data_paths("ephemeral")
        assert M.TRADES_DIR == tmp_path / "ephemeral"


# ── _trades_file_for_today ──────────────────────────────────


class TestTradesFileForToday:
    def test_returns_dated_path(self, tmp_path):
        M.TRADES_DIR = tmp_path
        f = _trades_file_for_today()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert f.name == f"trades_{today}.jsonl"


# ── _get_bankroll ───────────────────────────────────────────


class TestGetBankroll:
    def test_paper_uses_config(self):
        assert _get_bankroll({"bankroll": 5000}, "paper", None) == 5000.0

    def test_live_no_client_uses_config(self):
        assert _get_bankroll({"bankroll": 5000}, "live", None) == 5000.0

    def test_live_with_client_reads_balance(self):
        client = MagicMock()
        client.get_usdc_balance.return_value = 10_000_000_000  # 10000 USDC (6 decimals)
        balance = _get_bankroll({"bankroll": 5000}, "live", client)
        assert balance == 10000.0

    def test_live_client_error_falls_back(self):
        client = MagicMock()
        client.get_usdc_balance.side_effect = Exception("connection error")
        balance = _get_bankroll({"bankroll": 5000}, "live", client)
        assert balance == 5000.0


# ── _guess_league_cfg ───────────────────────────────────────


class TestGuessLeagueCfg:
    LEAGUES = {
        "epl": {"polymarket_tag": 82, "api_football_id": 39, "name": "Premier League"},
    }

    def test_known_slug_prefix(self):
        key, cfg = _guess_league_cfg("epl-ars-che-2026-03-22", self.LEAGUES)
        assert cfg["api_football_id"] == 39

    def test_unknown_slug(self):
        key, cfg = _guess_league_cfg("xyz-team1-team2-2026-01-01", self.LEAGUES)
        assert key == "xyz"

    def test_korean_league(self):
        key, cfg = _guess_league_cfg("kor-team1-team2-2026-01-01", self.LEAGUES)
        assert cfg["api_football_id"] == 136


# ── discover_football_events ────────────────────────────────


class TestDiscoverFootballEvents:
    def test_filters_moneyline_only(self):
        gamma = MagicMock()
        m_moneyline = _make_market(smt="moneyline")
        m_totals = _make_market(smt="totals")
        ev_with = _make_event(event_id=1, markets=[m_moneyline])
        ev_without = _make_event(event_id=2, markets=[m_totals])
        gamma.get_events.return_value = [ev_with, ev_without]

        leagues = {"epl": {"polymarket_tag": 82, "api_football_id": 39, "name": "EPL"}}
        results = discover_football_events(gamma, leagues)
        assert len(results) >= 1
        assert results[0][0].id == 1

    def test_deduplicates_events(self):
        gamma = MagicMock()
        m = _make_market()
        ev = _make_event(event_id=99, markets=[m])
        gamma.get_events.return_value = [ev, ev]  # duplicate

        results = discover_football_events(gamma, {"a": {"polymarket_tag": 1, "api_football_id": 1, "name": "A"}})
        ids = [r[0].id for r in results]
        assert ids.count(99) == 1

    def test_empty_markets_skipped(self):
        gamma = MagicMock()
        ev = _make_event(event_id=1, markets=[])
        gamma.get_events.return_value = [ev]

        results = discover_football_events(gamma, {"a": {"polymarket_tag": 1, "api_football_id": 1, "name": "A"}})
        assert len(results) == 0


# ── check_liquidity ─────────────────────────────────────────


class TestCheckLiquidity:
    def test_midpoint_exists_market_active(self):
        clob_ro = MagicMock()
        mid = MagicMock()
        mid.value = 0.85
        clob_ro.get_midpoint.return_value = mid

        book = MagicMock()
        book.asks = []
        book.bids = []
        clob_ro.get_order_book.return_value = book

        liq = check_liquidity(clob_ro, "token1", "NO", 1000, 0.85)
        assert liq.sufficient is True

    def test_no_midpoint_no_book_insufficient(self):
        clob_ro = MagicMock()
        clob_ro.get_midpoint.side_effect = Exception("404")

        book = MagicMock()
        book.asks = []
        book.bids = []
        clob_ro.get_order_book.return_value = book

        liq = check_liquidity(clob_ro, "token1", "NO", 1000, 0.85)
        assert liq.sufficient is False

    def test_orderbook_error_returns_empty(self):
        clob_ro = MagicMock()
        clob_ro.get_midpoint.side_effect = Exception("err")
        clob_ro.get_order_book.side_effect = Exception("err")

        liq = check_liquidity(clob_ro, "token1", "NO", 100, 0.50)
        assert liq.sufficient is False
        assert liq.available_shares == 0

    def test_native_asks_sufficient(self):
        clob_ro = MagicMock()
        mid = MagicMock()
        mid.value = 0.85
        clob_ro.get_midpoint.return_value = mid

        ask1 = MagicMock()
        ask1.price = 0.85
        ask1.size = 2000.0
        book = MagicMock()
        book.asks = [ask1]
        book.bids = [MagicMock(price=0.83)]
        clob_ro.get_order_book.return_value = book

        liq = check_liquidity(clob_ro, "token1", "NO", 1000, 0.85)
        assert liq.sufficient is True
        assert liq.available_shares >= 1000


# ── place_order ─────────────────────────────────────────────


class TestPlaceOrder:
    def test_paper_mode_returns_true(self):
        signal = TradeSignal(action="BUY", reason="test", side="NO",
                             poly_price=0.85, shares=100, stake=85.0)
        assert place_order(signal, None, "paper") is True

    def test_live_mode_no_client_returns_false(self):
        signal = TradeSignal(action="BUY", reason="test", side="NO",
                             poly_price=0.85, shares=100, stake=85.0)
        assert place_order(signal, None, "live") is False


# ── record_open_position ────────────────────────────────────


class TestRecordOpenPosition:
    def test_adds_to_open_positions(self, tmp_path):
        M.TRADES_DIR = tmp_path
        M.STATE_FILE = tmp_path / "state.json"
        M._open_positions = {}
        M._session_trades = 0

        signal = TradeSignal(
            action="BUY", reason="test", market_question="Will A win?",
            side="NO", token_id="tok1", poly_price=0.85, true_prob=0.90,
            book_implied=0.0, edge_pct=5.0, stake=500, shares=588,
            profit_if_win=88.2, loss_if_lose=499.8, expected_value=30.0,
        )
        record_open_position(1, signal, "A vs B", 80, "0-0", "A", "B")

        assert 1 in M._open_positions
        assert M._open_positions[1]["stake"] == 500
        assert M._open_positions[1]["side"] == "NO"
        assert M._session_trades == 1

    def test_saves_state_after_recording(self, tmp_path):
        M.TRADES_DIR = tmp_path
        M.STATE_FILE = tmp_path / "state.json"
        M._open_positions = {}
        M._session_trades = 0

        signal = TradeSignal(action="BUY", reason="t", market_question="X",
                             side="YES", token_id="t", poly_price=0.5,
                             true_prob=0.6, stake=100, shares=200,
                             profit_if_win=100, loss_if_lose=100, expected_value=20)
        record_open_position(99, signal, "X vs Y", 82, "1-0", "X", "Y")

        assert M.STATE_FILE.exists()


# ── resolve_ended_matches ───────────────────────────────────


class TestResolveEndedMatches:
    def setup_method(self):
        M._open_positions = {}
        M._session_pnl = 0.0
        M._session_wins = 0
        M._session_losses = 0

    def test_resolves_win(self, tmp_path):
        M.TRADES_DIR = tmp_path
        M.STATE_FILE = tmp_path / "state.json"

        M._open_positions = {
            1: {
                "event_title": "A vs B", "opened_at": "2026-01-01T00:00:00Z",
                "minute": 80, "score_at_entry": "1-0",
                "home_team": "teamA", "away_team": "teamB",
                "market_question": "Will teamB win?", "side": "NO",
                "token_id": "tok", "condition_id": "cond", "neg_risk": True,
                "poly_price": 0.85, "true_prob": 0.90, "book_implied": 0.0,
                "edge_pct": 5.0, "stake": 500, "shares": 588,
                "profit_if_win": 88.2, "loss_if_lose": 499.8,
                "expected_value": 30.0, "league": "EPL",
            }
        }

        ev = _make_event(event_id=1, ended=True, score="1-0", period="FT")
        events = [(ev, "epl", {})]

        resolve_ended_matches(events, None, "paper", False)

        assert len(M._open_positions) == 0
        assert M._session_wins == 1
        assert M._session_pnl == pytest.approx(88.2)

    def test_resolves_loss(self, tmp_path):
        M.TRADES_DIR = tmp_path
        M.STATE_FILE = tmp_path / "state.json"

        M._open_positions = {
            2: {
                "event_title": "A vs B", "opened_at": "2026-01-01T00:00:00Z",
                "minute": 80, "score_at_entry": "0-0",
                "home_team": "teamA", "away_team": "teamB",
                "market_question": "Will teamB win?", "side": "NO",
                "token_id": "tok", "condition_id": "cond", "neg_risk": True,
                "poly_price": 0.85, "true_prob": 0.90, "book_implied": 0.0,
                "edge_pct": 5.0, "stake": 500, "shares": 588,
                "profit_if_win": 88.2, "loss_if_lose": 499.8,
                "expected_value": 30.0, "league": "EPL",
            }
        }

        ev = _make_event(event_id=2, ended=True, score="0-2", period="FT")
        events = [(ev, "epl", {})]

        resolve_ended_matches(events, None, "paper", False)

        assert len(M._open_positions) == 0
        assert M._session_losses == 1
        assert M._session_pnl == pytest.approx(-499.8)

    def test_ignores_live_matches(self, tmp_path):
        M.TRADES_DIR = tmp_path
        M.STATE_FILE = tmp_path / "state.json"

        M._open_positions = {
            3: {"event_title": "A vs B", "market_question": "Will A win?",
                "side": "YES", "home_team": "a", "away_team": "b",
                "stake": 100, "shares": 100, "profit_if_win": 50,
                "loss_if_lose": 100, "poly_price": 0.5}
        }

        ev = _make_event(event_id=3, live=True, ended=False, score="1-0", period="2H")
        events = [(ev, "epl", {})]

        resolve_ended_matches(events, None, "paper", False)
        assert 3 in M._open_positions  # should not be resolved

    def test_recognizes_post_period(self, tmp_path):
        M.TRADES_DIR = tmp_path
        M.STATE_FILE = tmp_path / "state.json"

        M._open_positions = {
            4: {
                "event_title": "A vs B", "opened_at": "2026-01-01T00:00:00Z",
                "minute": 80, "score_at_entry": "2-0",
                "home_team": "teamA", "away_team": "teamB",
                "market_question": "Will teamA win?", "side": "YES",
                "token_id": "tok", "condition_id": "", "neg_risk": True,
                "poly_price": 0.90, "true_prob": 0.95, "book_implied": 0.0,
                "edge_pct": 5.0, "stake": 900, "shares": 1000,
                "profit_if_win": 100, "loss_if_lose": 900,
                "expected_value": 50.0, "league": "La Liga",
            }
        }

        ev = _make_event(event_id=4, ended=False, score="2-0", period="POST")
        events = [(ev, "la_liga", {})]

        resolve_ended_matches(events, None, "paper", False)
        assert len(M._open_positions) == 0
        assert M._session_wins == 1


# ── redeem_winning_position ─────────────────────────────────


class TestRedeemWinningPosition:
    def test_paper_mode_logs_only(self):
        pos = {"shares": 1000, "condition_id": "cond", "neg_risk": True}
        # Should not raise
        redeem_winning_position(pos, None, "paper")

    def test_live_no_client_warns(self):
        pos = {"shares": 1000, "condition_id": "cond", "neg_risk": True}
        redeem_winning_position(pos, None, "live")  # should not crash

    def test_live_no_condition_id_warns(self):
        pos = {"shares": 1000, "condition_id": "", "neg_risk": True}
        client = MagicMock()
        redeem_winning_position(pos, client, "live")
        client.redeem_position.assert_not_called()

    def test_live_calls_redeem(self):
        pos = {"shares": 1000, "condition_id": "0xabc", "neg_risk": True}
        client = MagicMock()
        client.redeem_position.return_value = MagicMock(transaction_hash="0x123")
        redeem_winning_position(pos, client, "live")
        client.redeem_position.assert_called_once()


# ── print functions (smoke tests) ───────────────────────────


class TestPrintFunctions:
    def test_print_signal_buy(self, capsys):
        signal = TradeSignal(
            action="BUY", reason="Edge 3%", market_question="Will A win?",
            side="NO", poly_price=0.85, true_prob=0.88, edge_pct=3.0,
            kelly_fraction=5.0, stake=500, shares=588,
            profit_if_win=88.0, loss_if_lose=500.0, expected_value=20.0,
            adjustments=["test adj"],
        )
        print_signal(signal, "A vs B", 80, "1-0", "paper")
        out = capsys.readouterr().out
        assert "BUY" in out or "PAPER" in out
        assert "Edge" in out

    def test_print_signal_no_trade(self, capsys):
        signal = TradeSignal(action="NO_TRADE", reason="No edge")
        print_signal(signal, "A vs B", 80, "0-0", "paper")
        out = capsys.readouterr().out
        assert "NO_TRADE" in out

    def test_print_status(self, capsys):
        M._session_pnl = 126.09
        M._session_wins = 1
        M._session_losses = 0
        M._open_positions = {}
        # Should not crash
        print_status(5, 100, "paper", 30, 10000.0)


# ── _log_resolved_trade ─────────────────────────────────────


class TestLogResolvedTrade:
    def test_writes_jsonl(self, tmp_path):
        from main import _log_resolved_trade
        M.TRADES_DIR = tmp_path

        pos = {
            "opened_at": "2026-03-21T20:00:00Z", "event_title": "A vs B",
            "minute": 80, "score_at_entry": "1-0", "market_question": "Will B win?",
            "side": "NO", "poly_price": 0.85, "true_prob": 0.90,
            "book_implied": 0.0, "edge_pct": 5.0, "stake": 500, "shares": 588,
            "league": "EPL", "trade_type": "STANDARD",
            "liquidity_depth": 5000, "liquidity_spread": 0.02,
        }
        _log_resolved_trade(pos, "1-0", "WIN", 88.2)

        files = list(tmp_path.glob("trades_*.jsonl"))
        assert len(files) == 1
        line = json.loads(files[0].read_text().strip())
        assert line["outcome"] == "WIN"
        assert line["pnl"] == 88.2
        assert line["league"] == "EPL"
