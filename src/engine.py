"""
engine.py — Decision engine.

Approach: use score + clock + Polymarket prices directly.
No base rate database, no clamping. Simple logic:

  - What should the probability be given the score and minutes remaining?
  - Is Polymarket mispriced vs that estimate?
  - If yes, trade. If no, skip.

The "true probability" comes from a simple model:
  - Minutes remaining → how likely is a goal?
  - Goal difference → how safe is the lead?
  - Compare our estimate to Polymarket price → edge or no edge
"""

import logging
from dataclasses import dataclass

from stats import BookmakerOdds, MatchStats

log = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    action: str  # "BUY" or "NO_TRADE"
    reason: str
    market_question: str = ""
    side: str = ""  # "YES" or "NO"
    token_id: str = ""

    # Probabilities
    true_prob: float = 0.0
    poly_price: float = 0.0
    book_implied: float = 0.0

    # Sizing
    edge_pct: float = 0.0
    kelly_fraction: float = 0.0
    stake: float = 0.0
    shares: int = 0
    profit_if_win: float = 0.0
    loss_if_lose: float = 0.0
    expected_value: float = 0.0

    # Context
    adjustments: list[str] | None = None


# ── Simple probability model ───────────────────────────────
#
# Based on one key stat: the probability of a goal being scored
# in the remaining minutes. In top-flight football:
#   ~2.7 goals per match → ~0.03 goals per minute
#   But late-game has slightly more goals (fatigue, urgency)
#   → ~0.033 goals per minute from min 75+
#
# P(at least one goal in N minutes) ≈ 1 - e^(-rate * N)
# P(trailing team scores) ≈ P(any goal) × 0.45 (rough share for non-leading team)
#
# This is simple but honest — it's real math, not made-up percentages.

import math

GOAL_RATE_PER_MINUTE = 0.033  # slightly elevated for late game


def _estimate_probability(
    home_goals: int, away_goals: int, minute: int,
    poly_home_prob: float, poly_away_prob: float,
) -> tuple[float, float, float, list[str]]:
    """Estimate match outcome probabilities from score + clock.

    Uses Polymarket pre-match prices as the team strength indicator,
    then adjusts based on the current score and time remaining.

    Returns (home_win, draw, away_win, explanations).
    """
    minutes_left = max(95 - minute, 1)  # include ~5 min stoppage
    goal_diff = home_goals - away_goals
    explanations = []

    # Probability of at least one more goal
    p_goal = 1.0 - math.exp(-GOAL_RATE_PER_MINUTE * minutes_left)
    explanations.append(f"min={minute}, {minutes_left}min left, P(goal)={p_goal:.1%}")

    if goal_diff == 0:
        # Tied — result depends on who scores next (or nobody)
        p_no_goal = 1.0 - p_goal  # stays draw
        # If a goal happens, split based on relative strength
        # Use Polymarket pre-match prices as strength proxy
        total_strength = poly_home_prob + poly_away_prob
        if total_strength > 0:
            home_share = poly_home_prob / total_strength
            away_share = poly_away_prob / total_strength
        else:
            home_share = 0.5
            away_share = 0.5

        home_win = p_goal * home_share * 0.5  # need to score AND opponent doesn't equalize
        away_win = p_goal * away_share * 0.5
        draw = 1.0 - home_win - away_win

        explanations.append(f"tied: strength H={home_share:.0%} A={away_share:.0%}")

    elif abs(goal_diff) == 1:
        # 1-goal lead — leading team needs to hold, trailing needs to score
        # P(trailing team equalizes) ≈ P(goal) × trailing_share
        total_strength = poly_home_prob + poly_away_prob
        if total_strength > 0:
            trailing_is_away = goal_diff > 0
            if trailing_is_away:
                trailing_share = poly_away_prob / total_strength
            else:
                trailing_share = poly_home_prob / total_strength
        else:
            trailing_share = 0.45

        # Probability trailing team equalizes
        p_equalize = p_goal * trailing_share * 0.6  # 0.6 = they need to be the one to score
        # Probability trailing team wins (equalize + score again) — very low
        p_trailing_wins = p_equalize * 0.15
        # Probability leading team scores another
        p_leading_extends = p_goal * (1 - trailing_share) * 0.4

        leading_win = (1.0 - p_equalize) + p_leading_extends * 0.5
        leading_win = min(leading_win, 0.99)
        draw = p_equalize * 0.7  # equalizes but no winner
        trailing_win = p_trailing_wins

        # Normalize
        total = leading_win + draw + trailing_win
        leading_win /= total
        draw /= total
        trailing_win /= total

        if goal_diff > 0:
            home_win, away_win = leading_win, trailing_win
        else:
            home_win, away_win = trailing_win, leading_win

        explanations.append(f"1-goal lead: P(equalize)={p_equalize:.1%}, trailing_share={trailing_share:.0%}")

    else:
        # 2+ goal lead — very safe
        # P(trailing team comes back) drops exponentially with goal diff
        comeback_factor = 0.12 ** abs(goal_diff)  # 2 goals: 1.4%, 3 goals: 0.17%
        p_comeback = p_goal * comeback_factor

        leading_win = 1.0 - p_comeback * 1.5
        leading_win = max(leading_win, 0.90)
        draw = p_comeback * 0.3
        trailing_win = p_comeback

        # Normalize
        total = leading_win + draw + trailing_win
        leading_win /= total
        draw /= total
        trailing_win /= total

        if goal_diff > 0:
            home_win, away_win = leading_win, trailing_win
        else:
            home_win, away_win = trailing_win, leading_win

        explanations.append(f"{abs(goal_diff)}-goal lead: comeback={p_comeback:.2%}")

    explanations.append(f"estimate: H={home_win:.1%} D={draw:.1%} A={away_win:.1%}")
    return home_win, draw, away_win, explanations


def evaluate(
    home_team: str,
    away_team: str,
    home_goals: int,
    away_goals: int,
    minute: int,
    stats: MatchStats,
    odds: BookmakerOdds,
    markets: list[dict],
    bankroll: float,
    config_risk: dict,
) -> TradeSignal:
    """Main decision function. Returns a TradeSignal."""
    min_edge = config_risk.get("min_edge_pct", 2.0) / 100.0
    kelly_frac = config_risk.get("kelly_fraction", 0.25)
    max_stake = config_risk.get("max_single_stake", 1000)

    goal_diff = home_goals - away_goals

    # Get Polymarket's view of team strength from market prices
    poly_home = 0.0
    poly_away = 0.0
    for m in markets:
        q = (m.get("question") or "").lower()
        prices = m.get("outcome_prices", [])
        if not prices or "draw" in q:
            continue
        yes_price = float(prices[0])
        if poly_home == 0:
            poly_home = yes_price
        else:
            poly_away = yes_price

    # Compute our estimate
    home_pct, draw_pct, away_pct, explanations = _estimate_probability(
        home_goals, away_goals, minute, poly_home, poly_away,
    )

    log.info(f"Estimate: H={home_pct:.1%} D={draw_pct:.1%} A={away_pct:.1%} | Poly: H={poly_home:.1%} A={poly_away:.1%}")

    # ── Apply Strategy Rules ──

    target_question = None
    target_side = None
    true_prob_for_trade = 0.0

    if abs(goal_diff) >= 2:
        # Buy the leading team to win
        if goal_diff > 0:
            target_question = _find_market(markets, home_team, "win")
            true_prob_for_trade = home_pct
        else:
            target_question = _find_market(markets, away_team, "win")
            true_prob_for_trade = away_pct
        target_side = "YES"

    elif abs(goal_diff) == 1:
        # Buy "losing team NOT to win"
        if goal_diff > 0:
            target_question = _find_market(markets, away_team, "win")
            true_prob_for_trade = 1.0 - away_pct
        else:
            target_question = _find_market(markets, home_team, "win")
            true_prob_for_trade = 1.0 - home_pct
        target_side = "NO"

    elif goal_diff == 0:
        # Tied — buy "weaker side NOT to win"
        if away_pct <= home_pct:
            target_question = _find_market(markets, away_team, "win")
            true_prob_for_trade = 1.0 - away_pct
        else:
            target_question = _find_market(markets, home_team, "win")
            true_prob_for_trade = 1.0 - home_pct
        target_side = "NO"

    if target_question is None:
        return TradeSignal(action="NO_TRADE", reason="Could not find matching market")

    # Get Polymarket price for our side
    market = target_question
    prices = market.get("outcome_prices", [])
    token_ids = market.get("token_ids", [])

    if target_side == "YES":
        poly_price = prices[0] if prices else 0
        token_id = token_ids[0] if token_ids else ""
    else:
        poly_price = prices[1] if len(prices) > 1 else 0
        token_id = token_ids[1] if len(token_ids) > 1 else ""

    if poly_price <= 0 or poly_price >= 1:
        return TradeSignal(action="NO_TRADE", reason=f"Invalid Polymarket price: {poly_price}")

    # Compute edge — our estimate vs Polymarket price, no clamping
    edge = true_prob_for_trade - poly_price

    if edge <= 0:
        return TradeSignal(
            action="NO_TRADE",
            reason=f"No edge: estimate={true_prob_for_trade:.1%} poly={poly_price:.1%} edge={edge:.1%}",
            true_prob=true_prob_for_trade,
            poly_price=poly_price,
            edge_pct=edge * 100,
            adjustments=explanations,
        )

    if edge < min_edge:
        return TradeSignal(
            action="NO_TRADE",
            reason=f"Edge too small: {edge:.1%} < min {min_edge:.1%}",
            true_prob=true_prob_for_trade,
            poly_price=poly_price,
            edge_pct=edge * 100,
            adjustments=explanations,
        )

    # Kelly sizing
    kelly_full = (true_prob_for_trade - poly_price) / (1.0 - poly_price)
    kelly_sized = kelly_full * kelly_frac
    stake = min(bankroll * kelly_sized, max_stake)
    stake = max(stake, 0)

    if stake < 1:
        return TradeSignal(action="NO_TRADE", reason="Stake too small after Kelly sizing")

    shares = int(stake / poly_price)
    profit_if_win = shares * (1.0 - poly_price)
    loss_if_lose = shares * poly_price
    ev = true_prob_for_trade * profit_if_win - (1.0 - true_prob_for_trade) * loss_if_lose

    return TradeSignal(
        action="BUY",
        reason=f"Edge {edge:.1%} on {target_side} {market.get('question', '')}",
        market_question=market.get("question", ""),
        side=target_side,
        token_id=token_id,
        true_prob=true_prob_for_trade,
        poly_price=poly_price,
        book_implied=0.0,
        edge_pct=edge * 100,
        kelly_fraction=kelly_sized * 100,
        stake=round(stake, 2),
        shares=shares,
        profit_if_win=round(profit_if_win, 2),
        loss_if_lose=round(loss_if_lose, 2),
        expected_value=round(ev, 2),
        adjustments=explanations,
    )


def _find_market(markets: list[dict], team_name: str, keyword: str) -> dict | None:
    """Find a market matching a team name and keyword (e.g. 'win')."""
    team_lower = team_name.lower()
    for m in markets:
        q = m.get("question", "").lower()
        if team_lower[:4] in q and keyword in q:
            return m
    for word in team_lower.split():
        if len(word) < 3:
            continue
        for m in markets:
            q = m.get("question", "").lower()
            if word in q and keyword in q:
                return m
    return None
