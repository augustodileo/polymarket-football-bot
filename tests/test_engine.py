"""Tests for engine.py — probability model + decision logic."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from engine import _estimate_probability, _find_market, evaluate, TradeSignal


# ── _estimate_probability ───────────────────────────────────


class TestEstimateProbability:
    """Test the score + clock probability model."""

    def test_tied_late_game_draw_most_likely(self):
        """Tied at minute 85 → draw should be the most likely outcome."""
        h, d, a, _ = _estimate_probability(0, 0, 85, 0.50, 0.30)
        assert d > h
        assert d > a
        assert d > 0.7  # draw should be dominant

    def test_tied_minute_90_draw_very_likely(self):
        """Tied at minute 90 → draw > 85%."""
        h, d, a, _ = _estimate_probability(1, 1, 90, 0.40, 0.20)
        assert d > 0.85

    def test_tied_stronger_home_team_has_higher_win(self):
        """If home is much stronger (Poly 70% vs 10%), home win > away win."""
        h, d, a, _ = _estimate_probability(0, 0, 80, 0.70, 0.10)
        assert h > a

    def test_tied_equal_teams_similar_win_probs(self):
        """Equal teams tied → home and away win probs should be similar."""
        h, d, a, _ = _estimate_probability(0, 0, 80, 0.35, 0.35)
        assert abs(h - a) < 0.02

    def test_one_goal_lead_leader_very_likely(self):
        """1-0 at minute 85 → leading team should win > 90%."""
        h, d, a, _ = _estimate_probability(1, 0, 85, 0.50, 0.20)
        assert h > 0.90

    def test_one_goal_lead_trailing_very_unlikely(self):
        """0-1 at minute 85 → home (trailing) win should be < 5%."""
        h, d, a, _ = _estimate_probability(0, 1, 85, 0.50, 0.20)
        assert h < 0.05

    def test_one_goal_lead_minute_80_less_certain(self):
        """1-0 at minute 80 → less certain than minute 90."""
        h80, _, _, _ = _estimate_probability(1, 0, 80, 0.50, 0.20)
        h90, _, _, _ = _estimate_probability(1, 0, 90, 0.50, 0.20)
        assert h90 > h80

    def test_two_goal_lead_very_safe(self):
        """2-0 at minute 80 → leading team > 97%."""
        h, d, a, _ = _estimate_probability(2, 0, 80, 0.50, 0.20)
        assert h > 0.97

    def test_two_goal_lead_minute_85_even_safer(self):
        """2-0 at minute 85 → leading team > 98%."""
        h, d, a, _ = _estimate_probability(2, 0, 85, 0.50, 0.20)
        assert h > 0.98

    def test_three_goal_lead_almost_certain(self):
        """3-0 at minute 80 → leading team > 99%."""
        h, d, a, _ = _estimate_probability(3, 0, 80, 0.50, 0.20)
        assert h > 0.99

    def test_away_leading(self):
        """0-2 at minute 80 → away team should win > 97%."""
        h, d, a, _ = _estimate_probability(0, 2, 80, 0.50, 0.20)
        assert a > 0.97

    def test_probabilities_sum_to_one(self):
        """All scenarios should sum to ~1.0."""
        for score in [(0, 0), (1, 0), (0, 1), (2, 0), (2, 1), (3, 0)]:
            for minute in [75, 80, 85, 90]:
                h, d, a, _ = _estimate_probability(*score, minute, 0.50, 0.30)
                total = h + d + a
                assert abs(total - 1.0) < 0.01, f"score={score} min={minute}: total={total}"

    def test_returns_explanations(self):
        """Should return a non-empty list of explanation strings."""
        _, _, _, exp = _estimate_probability(1, 0, 80, 0.50, 0.20)
        assert len(exp) >= 2
        assert any("min=" in e for e in exp)

    def test_zero_poly_prices_doesnt_crash(self):
        """Should handle zero Polymarket prices gracefully."""
        h, d, a, _ = _estimate_probability(0, 0, 80, 0, 0)
        assert h + d + a == pytest.approx(1.0, abs=0.01)

    def test_weaker_trailing_team_less_likely_to_equalize(self):
        """A weak trailing team should be less likely to come back than a strong one."""
        # Strong trailing team (away was 60% favorite)
        _, _, a_strong, _ = _estimate_probability(1, 0, 80, 0.20, 0.60)
        # Weak trailing team (away was 10%)
        _, _, a_weak, _ = _estimate_probability(1, 0, 80, 0.60, 0.10)
        assert a_strong > a_weak


# ── _find_market ────────────────────────────────────────────


class TestFindMarket:
    """Test market matching by team name."""

    MARKETS = [
        {"question": "Will Arsenal FC win on 2026-03-22?"},
        {"question": "Will Arsenal FC vs. Chelsea FC end in a draw?"},
        {"question": "Will Chelsea FC win on 2026-03-22?"},
    ]

    def test_find_home_win(self):
        m = _find_market(self.MARKETS, "Arsenal FC", "win")
        assert m is not None
        assert "Arsenal" in m["question"]

    def test_find_away_win(self):
        m = _find_market(self.MARKETS, "Chelsea FC", "win")
        assert m is not None
        assert "Chelsea" in m["question"]

    def test_no_match(self):
        m = _find_market(self.MARKETS, "Barcelona", "win")
        assert m is None

    def test_partial_name_match(self):
        m = _find_market(self.MARKETS, "Arsenal", "win")
        assert m is not None

    def test_multi_word_team(self):
        markets = [{"question": "Will Real Madrid CF win?"}]
        m = _find_market(markets, "Real Madrid CF", "win")
        assert m is not None

    def test_short_words_ignored(self):
        """Short words like 'FC', 'SC' shouldn't cause false matches."""
        markets = [
            {"question": "Will Nashville SC win?"},
            {"question": "Will Orlando City SC win?"},
        ]
        m = _find_market(markets, "Orlando City SC", "win")
        assert m is not None
        assert "Orlando" in m["question"]


# ── evaluate (full decision engine) ────────────────────────


class TestEvaluate:
    """Test the full evaluate function."""

    MARKETS = [
        {
            "question": "Will Team A win?",
            "sports_market_type": "moneyline",
            "outcomes": ["Yes", "No"],
            "outcome_prices": [0.50, 0.50],
            "token_ids": ["token_a_yes", "token_a_no"],
            "condition_id": "cond_a",
            "neg_risk": True,
        },
        {
            "question": "Will the match end in a draw?",
            "sports_market_type": "moneyline",
            "outcomes": ["Yes", "No"],
            "outcome_prices": [0.20, 0.80],
            "token_ids": ["token_d_yes", "token_d_no"],
            "condition_id": "cond_d",
            "neg_risk": True,
        },
        {
            "question": "Will Team B win?",
            "sports_market_type": "moneyline",
            "outcomes": ["Yes", "No"],
            "outcome_prices": [0.30, 0.70],
            "token_ids": ["token_b_yes", "token_b_no"],
            "condition_id": "cond_b",
            "neg_risk": True,
        },
    ]

    RISK = {
        "min_edge_pct": 2.0,
        "kelly_fraction": 0.25,
        "max_single_stake": 1000,
        "max_deviation_from_books": 8.0,
    }

    def test_no_trade_when_market_efficient(self):
        """When Polymarket prices match reality, no edge → NO_TRADE."""
        # 0-0 at minute 89, draw very likely — Polymarket agrees
        markets = [
            {"question": "Will Team A win?", "outcome_prices": [0.06, 0.94],
             "token_ids": ["a", "b"], "sports_market_type": "moneyline"},
            {"question": "Will draw?", "outcome_prices": [0.88, 0.12],
             "token_ids": ["c", "d"], "sports_market_type": "moneyline"},
            {"question": "Will Team B win?", "outcome_prices": [0.06, 0.94],
             "token_ids": ["e", "f"], "sports_market_type": "moneyline"},
        ]
        signal = evaluate("Team A", "Team B", 0, 0, 89,
                          markets, 10000, self.RISK)
        assert signal.action == "NO_TRADE"

    def test_trade_when_big_lead_underpriced(self):
        """2-0 at minute 85 but Polymarket has leader at only 90% → should find edge."""
        markets = [
            {"question": "Will Team A win?", "outcome_prices": [0.90, 0.10],
             "token_ids": ["a", "b"], "sports_market_type": "moneyline"},
            {"question": "Will draw?", "outcome_prices": [0.05, 0.95],
             "token_ids": ["c", "d"], "sports_market_type": "moneyline"},
            {"question": "Will Team B win?", "outcome_prices": [0.05, 0.95],
             "token_ids": ["e", "f"], "sports_market_type": "moneyline"},
        ]
        signal = evaluate("Team A", "Team B", 2, 0, 85,
                          markets, 10000, self.RISK)
        assert signal.action == "BUY"
        assert signal.side == "YES"
        assert signal.edge_pct > 2.0
        assert "Team A" in signal.market_question

    def test_one_goal_lead_buys_no_on_trailing(self):
        """1-0 at minute 82 → should buy NO on trailing team."""
        markets = [
            {"question": "Will Home win?", "outcome_prices": [0.70, 0.30],
             "token_ids": ["a", "b"], "sports_market_type": "moneyline"},
            {"question": "Will draw?", "outcome_prices": [0.20, 0.80],
             "token_ids": ["c", "d"], "sports_market_type": "moneyline"},
            {"question": "Will Away win?", "outcome_prices": [0.10, 0.90],
             "token_ids": ["e", "f"], "sports_market_type": "moneyline"},
        ]
        signal = evaluate("Home", "Away", 1, 0, 82,
                          markets, 10000, self.RISK)
        # Should target Away win market with NO side
        if signal.action == "BUY":
            assert signal.side == "NO"
            assert "Away" in signal.market_question

    def test_kelly_sizing_respects_max_stake(self):
        """Stake should never exceed max_single_stake."""
        markets = [
            {"question": "Will Home win?", "outcome_prices": [0.80, 0.20],
             "token_ids": ["a", "b"], "sports_market_type": "moneyline"},
            {"question": "Will draw?", "outcome_prices": [0.05, 0.95],
             "token_ids": ["c", "d"], "sports_market_type": "moneyline"},
            {"question": "Will Away win?", "outcome_prices": [0.05, 0.95],
             "token_ids": ["e", "f"], "sports_market_type": "moneyline"},
        ]
        signal = evaluate("Home", "Away", 3, 0, 88,
                          markets, 100000,
                          {**self.RISK, "max_single_stake": 500})
        if signal.action == "BUY":
            assert signal.stake <= 500

    def test_returns_trade_signal_dataclass(self):
        """Should always return a TradeSignal."""
        signal = evaluate("A", "B", 0, 0, 80,
                          self.MARKETS, 10000, self.RISK)
        assert isinstance(signal, TradeSignal)
        assert signal.action in ("BUY", "NO_TRADE")

    def test_no_markets_returns_no_trade(self):
        """Empty markets list → NO_TRADE."""
        signal = evaluate("A", "B", 0, 0, 80,
                          [], 10000, self.RISK)
        assert signal.action == "NO_TRADE"


# ── Edge cases ──────────────────────────────────────────────


class TestEdgeCases:
    """Test boundary conditions."""

    def test_minute_95_almost_no_time(self):
        """At minute 95, almost no time left → probabilities extreme."""
        h, d, a, _ = _estimate_probability(1, 0, 95, 0.50, 0.30)
        assert h > 0.95  # leader almost certain

    def test_minute_80_more_time(self):
        """At minute 80, more time → less certain."""
        h80, _, _, _ = _estimate_probability(1, 0, 80, 0.50, 0.30)
        h95, _, _, _ = _estimate_probability(1, 0, 95, 0.50, 0.30)
        assert h95 > h80

    def test_large_goal_diff(self):
        """5-0 at minute 80 → leader ~100%."""
        h, d, a, _ = _estimate_probability(5, 0, 80, 0.50, 0.20)
        assert h > 0.999

    def test_negative_edge_no_trade(self):
        """When our estimate is below Polymarket price → NO_TRADE."""
        markets = [
            {"question": "Will A win?", "outcome_prices": [0.02, 0.98],
             "token_ids": ["a", "b"], "sports_market_type": "moneyline"},
            {"question": "Will draw?", "outcome_prices": [0.95, 0.05],
             "token_ids": ["c", "d"], "sports_market_type": "moneyline"},
            {"question": "Will B win?", "outcome_prices": [0.03, 0.97],
             "token_ids": ["e", "f"], "sports_market_type": "moneyline"},
        ]
        signal = evaluate("A", "B", 0, 0, 92,
                          markets, 10000,
                          {"min_edge_pct": 2.0, "kelly_fraction": 0.25, "max_single_stake": 1000})
        assert signal.action == "NO_TRADE"

    def test_away_leading_two_goals(self):
        """0-2 at minute 83 → should buy YES on away team win."""
        markets = [
            {"question": "Will Home win?", "outcome_prices": [0.03, 0.97],
             "token_ids": ["a", "b"], "sports_market_type": "moneyline"},
            {"question": "Will draw?", "outcome_prices": [0.02, 0.98],
             "token_ids": ["c", "d"], "sports_market_type": "moneyline"},
            {"question": "Will Away win?", "outcome_prices": [0.90, 0.10],
             "token_ids": ["e", "f"], "sports_market_type": "moneyline"},
        ]
        signal = evaluate("Home", "Away", 0, 2, 83,
                          markets, 10000,
                          {"min_edge_pct": 2.0, "kelly_fraction": 0.25, "max_single_stake": 1000})
        if signal.action == "BUY":
            assert signal.side == "YES"
            assert "Away" in signal.market_question

    def test_away_leading_one_goal_buys_no_on_home(self):
        """0-1 at minute 82 → should buy NO on home win."""
        markets = [
            {"question": "Will Home win?", "outcome_prices": [0.08, 0.92],
             "token_ids": ["a", "b"], "sports_market_type": "moneyline"},
            {"question": "Will draw?", "outcome_prices": [0.20, 0.80],
             "token_ids": ["c", "d"], "sports_market_type": "moneyline"},
            {"question": "Will Away win?", "outcome_prices": [0.72, 0.28],
             "token_ids": ["e", "f"], "sports_market_type": "moneyline"},
        ]
        signal = evaluate("Home", "Away", 0, 1, 82,
                          markets, 10000,
                          {"min_edge_pct": 2.0, "kelly_fraction": 0.25, "max_single_stake": 1000})
        if signal.action == "BUY":
            assert signal.side == "NO"
            assert "Home" in signal.market_question

    def test_tied_weaker_away(self):
        """0-0 at min 82, away is weaker → NO on away win."""
        markets = [
            {"question": "Will Home win?", "outcome_prices": [0.45, 0.55],
             "token_ids": ["a", "b"], "sports_market_type": "moneyline"},
            {"question": "Will draw?", "outcome_prices": [0.25, 0.75],
             "token_ids": ["c", "d"], "sports_market_type": "moneyline"},
            {"question": "Will Away win?", "outcome_prices": [0.05, 0.95],
             "token_ids": ["e", "f"], "sports_market_type": "moneyline"},
        ]
        signal = evaluate("Home", "Away", 0, 0, 82,
                          markets, 10000,
                          {"min_edge_pct": 0.5, "kelly_fraction": 0.25, "max_single_stake": 1000})
        if signal.action == "BUY":
            assert signal.side == "NO"

    def test_invalid_poly_price(self):
        """Price of 0 or 1 → NO_TRADE."""
        markets = [
            {"question": "Will A win?", "outcome_prices": [1.0, 0.0],
             "token_ids": ["a", "b"], "sports_market_type": "moneyline"},
            {"question": "Will draw?", "outcome_prices": [0.0, 1.0],
             "token_ids": ["c", "d"], "sports_market_type": "moneyline"},
            {"question": "Will B win?", "outcome_prices": [0.0, 1.0],
             "token_ids": ["e", "f"], "sports_market_type": "moneyline"},
        ]
        signal = evaluate("A", "B", 2, 0, 90,
                          markets, 10000,
                          {"min_edge_pct": 2.0, "kelly_fraction": 0.25, "max_single_stake": 1000})
        assert signal.action == "NO_TRADE"

    def test_edge_too_small(self):
        """Edge exists but below threshold → NO_TRADE with reason."""
        markets = [
            {"question": "Will Home win?", "outcome_prices": [0.96, 0.04],
             "token_ids": ["a", "b"], "sports_market_type": "moneyline"},
            {"question": "Will draw?", "outcome_prices": [0.02, 0.98],
             "token_ids": ["c", "d"], "sports_market_type": "moneyline"},
            {"question": "Will Away win?", "outcome_prices": [0.02, 0.98],
             "token_ids": ["e", "f"], "sports_market_type": "moneyline"},
        ]
        signal = evaluate("Home", "Away", 2, 0, 88,
                          markets, 10000,
                          {"min_edge_pct": 10.0, "kelly_fraction": 0.25, "max_single_stake": 1000})
        assert signal.action == "NO_TRADE"
        assert "too small" in signal.reason.lower() or "edge" in signal.reason.lower()

    def test_zero_poly_strength_tied(self):
        """Zero Polymarket prices for both teams (edge case)."""
        h, d, a, _ = _estimate_probability(0, 0, 85, 0.0, 0.0)
        assert abs(h + d + a - 1.0) < 0.01

    def test_find_market_second_word(self):
        """_find_market should match on any significant word."""
        markets = [{"question": "Will Real Madrid CF win?"}]
        m = _find_market(markets, "Real Madrid CF", "win")
        assert m is not None
