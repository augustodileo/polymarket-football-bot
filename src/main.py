"""
main.py — Polymarket Football Trading Bot

Monitors live football matches via Polymarket's Gamma API.
When a match hits minute 80+, fetches detailed stats from API-Football,
computes edge using deterministic rules, and places trades via py-clob-client.

Paper mode is identical to live mode except the order is not placed.
All P&L tracking, bankroll updates, and result resolution work the same.

Usage:
    uv run main.py              # paper mode (default)
    uv run main.py --live       # real money (requires config.yaml: mode: live)
"""

import argparse
import json
from dataclasses import dataclass
import logging
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

# Project root is one level up from src/
_PROJECT_ROOT = Path(__file__).parent.parent

# Version injected at build time from git tag. See VERSION file.
_VERSION_FILE = _PROJECT_ROOT / "VERSION"
__version__ = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "dev"

import yaml
from polymarket_apis import PolymarketGammaClient, PolymarketClobClient, PolymarketReadOnlyClobClient

from engine import TradeSignal, evaluate
from stats import MatchStats, BookmakerOdds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
# Suppress noisy HTTP request logs from httpx/httpcore
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

BASE_DIR = _PROJECT_ROOT / "data"

# Set in run_loop() based on mode:
#   live  -> data/live/
#   paper -> data/paper/  (ephemeral, can be wiped anytime)
TRADES_DIR: Path = BASE_DIR
STATE_FILE: Path = BASE_DIR / "state.json"


def _init_data_paths(mode: str):
    """Set data paths based on mode. Paper and live data never mix."""
    global TRADES_DIR, STATE_FILE
    TRADES_DIR = BASE_DIR / mode
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE = TRADES_DIR / "state.json"


# ── State ───────────────────────────────────────────────────

# Events already evaluated (won't re-evaluate)
_evaluated_event_ids: set[int] = set()

# Open positions waiting for match to end: event_id → position info
_open_positions: dict[int, dict] = {}

# Session totals
_session_pnl: float = 0.0
_session_wins: int = 0
_session_losses: int = 0
_session_trades: int = 0


# ── State Persistence ──────────────────────────────────────


def _trades_file_for_today() -> Path:
    """Return the daily trades file path: trades_2026-03-21.jsonl"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return TRADES_DIR / f"trades_{today}.jsonl"


def save_state():
    """Save only what needs to survive a restart: open positions + cumulative P&L."""
    TRADES_DIR.mkdir(exist_ok=True)
    state = {
        "open_positions": {str(k): v for k, v in _open_positions.items()},
        "cumulative_pnl": _session_pnl,
        "cumulative_wins": _session_wins,
        "cumulative_losses": _session_losses,
        "cumulative_trades": _session_trades,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_state():
    """Load saved state from disk. Only restores open positions + cumulative P&L.

    Everything else (evaluated events, scheduled bets, prefetched data)
    starts fresh each run — stale data from a previous session would cause
    the bot to skip matches it should re-evaluate.
    """
    global _open_positions
    global _session_pnl, _session_wins, _session_losses, _session_trades

    if not STATE_FILE.exists():
        return

    try:
        with open(STATE_FILE) as f:
            state = json.load(f)

        _open_positions = {int(k): v for k, v in state.get("open_positions", {}).items()}
        _session_pnl = state.get("cumulative_pnl", state.get("session_pnl", 0.0))
        _session_wins = state.get("cumulative_wins", state.get("session_wins", 0))
        _session_losses = state.get("cumulative_losses", state.get("session_losses", 0))
        _session_trades = state.get("cumulative_trades", state.get("session_trades", 0))

        saved_at = state.get("saved_at", "unknown")
        log.info(f"Restored state from {saved_at}")
        if _open_positions:
            log.info(f"  {len(_open_positions)} open positions resumed:")
            for eid, pos in _open_positions.items():
                log.info(f"    {pos['event_title']} | {pos['side']} \"{pos['market_question']}\" | ${pos['stake']:.0f}")
        if _session_pnl != 0:
            log.info(f"  Cumulative P&L: {_session_wins}W-{_session_losses}L | ${_session_pnl:+.2f}")
    except Exception as e:
        log.warning(f"Could not load state: {e} — starting fresh")


# ── Config ──────────────────────────────────────────────────


def load_config() -> dict:
    config_path = _PROJECT_ROOT / "config.yaml"
    if not config_path.exists():
        log.error("config.yaml not found — copy config.yaml and fill in your keys")
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


# ── Market Discovery ────────────────────────────────────────


def _guess_league_cfg(event_slug: str, leagues: dict) -> tuple[str, dict]:
    """Try to match an event slug to a configured league. Returns (key, cfg) or a fallback."""
    slug = (event_slug or "").lower()
    # Polymarket slugs start with league code: "epl-ars-che-...", "lal-bar-ray-..."
    for league_key, league_cfg in leagues.items():
        # Match by polymarket tag slug prefix patterns
        tag = str(league_cfg.get("polymarket_tag", ""))
        if tag in str(getattr(event_slug, 'tags', '') or ''):
            return league_key, league_cfg
    # Fallback — use slug prefix to guess API-Football league ID for odds
    _SLUG_TO_LEAGUE = {
        "epl": 39, "lal": 140, "sea": 135, "bun": 78, "fl1": 61,
        "ucl": 2, "uel": 3, "mls": 253, "ere": 88, "por": 94,
        "kor": 136, "jap": 98, "bra": 71, "arg": 128, "mex": 262,
        "spl": 307, "chi": 169, "aus": 188, "ind": 323,
        "col": 239, "cdr": 143, "dfb": 81, "cde": 66, "itc": 137,
        "efa": 45, "efl": 46, "acn": 6, "con": 11, "cof": 15,
        "uef": 848, "caf": 12, "fif": 1, "rus": 235,
    }
    prefix = slug.split("-")[0] if "-" in slug else ""
    api_id = _SLUG_TO_LEAGUE.get(prefix, 0)
    return prefix or "_unknown", {"polymarket_tag": 0, "api_football_id": api_id, "name": prefix.upper() or "Unknown"}


def discover_football_events(gamma: PolymarketGammaClient, leagues: dict) -> list:
    """Find all active football match events across all leagues.

    Uses configured league tags first, then sweeps the global football tag (100350)
    to catch leagues that might be miscategorized or not in our config.
    """
    all_events = []
    seen_ids = set()

    # Pass 1: configured leagues (we know the API-Football mapping)
    for league_key, league_cfg in leagues.items():
        tag_id = league_cfg["polymarket_tag"]
        try:
            events = gamma.get_events(tag_id=tag_id, active=True, closed=False, limit=50)
            for e in events:
                if e.id in seen_ids:
                    continue
                if not e.markets:
                    continue
                has_moneyline = any(
                    m.sports_market_type == "moneyline" for m in e.markets
                )
                if not has_moneyline:
                    continue
                seen_ids.add(e.id)
                all_events.append((e, league_key, league_cfg))
        except Exception as ex:
            log.warning(f"Failed to fetch {league_key} events: {ex}")

    # Pass 2: global football tag — catches Korean league, cups, etc.
    try:
        global_events = gamma.get_events(tag_id=100350, active=True, closed=False, limit=100)
        new_count = 0
        for e in global_events:
            if e.id in seen_ids:
                continue
            if not e.markets:
                continue
            has_moneyline = any(
                m.sports_market_type == "moneyline" for m in e.markets
            )
            if not has_moneyline:
                continue
            seen_ids.add(e.id)
            league_key, league_cfg = _guess_league_cfg(e.slug, leagues)
            all_events.append((e, league_key, league_cfg))
            new_count += 1
        if new_count > 0:
            log.info(f"Global football scan found {new_count} extra matches not in configured leagues")
    except Exception as ex:
        log.warning(f"Failed global football scan: {ex}")

    return all_events


# ── Parse Event Data ────────────────────────────────────────


def parse_score(score_str: str | None) -> tuple[int, int]:
    """Parse '2-1' into (2, 1). Returns (0, 0) if unparseable."""
    if not score_str:
        return (0, 0)
    try:
        parts = str(score_str).split("-")
        return (int(parts[0].strip()), int(parts[1].strip()))
    except (ValueError, IndexError):
        return (0, 0)


def parse_teams_from_title(title: str) -> tuple[str, str]:
    """Extract home and away team names from event title."""
    if ":" in title:
        title = title.split(":", 1)[1].strip()
    for sep in ["vs.", " vs ", " v "]:
        if sep in title:
            parts = title.split(sep, 1)
            return (parts[0].strip(), parts[1].strip())
    return (title, "")


@dataclass
class LiquidityInfo:
    """Orderbook liquidity snapshot for a token."""
    available_shares: int = 0      # shares available within price tolerance
    best_price: float = 0.0        # best available price
    spread: float = 0.0            # bid-ask spread
    total_depth: float = 0.0       # total $ depth on our side
    levels: int = 0                # number of price levels
    sufficient: bool = False       # enough liquidity for our trade?


def check_liquidity(
    clob_ro: PolymarketReadOnlyClobClient,
    token_id: str,
    side: str,
    desired_shares: int,
    max_price: float,
    complement_token_id: str = "",
) -> LiquidityInfo:
    """Check orderbook liquidity for a token before placing a bet.

    Polymarket uses neg-risk markets where buying NO at 0.60 is matched by
    someone's YES bid at 0.40. So we check BOTH the native book AND the
    complement book for effective liquidity. Also checks if the midpoint
    exists (confirms the market is active).
    """
    try:
        # First: check if market has a midpoint (quick active check)
        midpoint = clob_ro.get_midpoint(token_id)
        if midpoint and midpoint.value and midpoint.value > 0:
            mid_val = midpoint.value
        else:
            mid_val = 0
    except Exception:
        mid_val = 0

    try:
        book = clob_ro.get_order_book(token_id)
    except Exception as e:
        log.warning(f"Could not fetch orderbook: {e}")
        return LiquidityInfo()

    # Native asks on our token (direct sellers)
    native_asks = book.asks or []
    native_bids = book.bids or []

    # Cross-book: complement token's bids become our effective asks
    # (someone bidding 0.40 on YES = offering to sell NO at 0.60)
    cross_asks = []
    if complement_token_id:
        try:
            comp_book = clob_ro.get_order_book(complement_token_id)
            for bid in (comp_book.bids or []):
                cross_asks.append(type(bid)(price=round(1.0 - bid.price, 4), size=bid.size))
            cross_asks.sort(key=lambda x: x.price)
        except Exception:
            pass

    # Merge native asks + cross asks, sorted by price
    all_asks = sorted(
        list(native_asks) + cross_asks,
        key=lambda x: x.price,
    )

    if not all_asks and mid_val <= 0:
        return LiquidityInfo()

    # If we have a midpoint but no orderbook depth, market is active
    # (orders may be placed via AMM or just-in-time liquidity)
    if not all_asks and mid_val > 0:
        return LiquidityInfo(
            best_price=mid_val,
            spread=0.02,  # estimate
            sufficient=True,  # trust the midpoint — market is active
        )

    best_ask = all_asks[0].price if all_asks else (mid_val or 0)
    best_bid = native_bids[0].price if native_bids else 0
    spread = best_ask - best_bid if best_ask and best_bid else 0

    # Count shares available within tolerance of our target price
    max_acceptable = max_price + 0.03
    available = 0
    total_depth = 0.0
    levels = 0
    for order in all_asks:
        if order.price > max_acceptable:
            break
        available += int(order.size)
        total_depth += order.size * order.price
        levels += 1

    # If native book looks empty but midpoint exists, market is active
    # This handles neg-risk markets where cross-matching happens server-side
    if available == 0 and mid_val > 0 and abs(mid_val - max_price) < 0.10:
        return LiquidityInfo(
            available_shares=desired_shares,
            best_price=mid_val,
            spread=round(spread, 4) if spread > 0 else 0.02,
            total_depth=desired_shares * mid_val,
            levels=1,
            sufficient=True,
        )

    # Accept partial fills — if at least 50% of desired shares are available,
    # or if total depth is >= $500, the market is tradeable (just reduce size)
    min_fill_ratio = 0.5
    min_depth_usd = 500
    sufficient = (
        available >= desired_shares
        or available >= desired_shares * min_fill_ratio
        or total_depth >= min_depth_usd
    )

    return LiquidityInfo(
        available_shares=available,
        best_price=best_ask,
        spread=round(spread, 4),
        total_depth=round(total_depth, 2),
        levels=levels,
        sufficient=sufficient,
    )


def markets_to_dicts(markets) -> list[dict]:
    """Convert Pydantic market objects to plain dicts for the engine."""
    result = []
    for m in markets:
        if m.sports_market_type != "moneyline":
            continue
        prices = m.outcome_prices
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except (json.JSONDecodeError, TypeError):
                prices = []
        if isinstance(prices, list):
            prices = [float(p) for p in prices]

        token_ids = m.token_ids
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except (json.JSONDecodeError, TypeError):
                token_ids = []

        outcomes = m.outcomes
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except (json.JSONDecodeError, TypeError):
                outcomes = []

        result.append({
            "question": m.question or "",
            "sports_market_type": m.sports_market_type,
            "outcomes": outcomes,
            "outcome_prices": prices,
            "token_ids": token_ids,
            "accepting_orders": m.accepting_orders,
            "condition_id": m.condition_id or "",
            "neg_risk": bool(m.neg_risk),
        })
    return result


# ── Trade Execution ─────────────────────────────────────────


def place_order(signal: TradeSignal, clob: PolymarketClobClient | None, mode: str) -> bool:
    """Place the order on Polymarket. In paper mode, skip the API call only."""
    if mode == "paper":
        log.info(f"[PAPER] BUY {signal.side} @ {signal.poly_price:.4f} "
                 f"x{signal.shares} shares = ${signal.stake:.2f}")
        return True

    if clob is None:
        log.error("CLOB client not initialized — cannot place order")
        return False

    try:
        from polymarket_apis import OrderArgs, OrderType
        order = OrderArgs(
            token_id=signal.token_id,
            price=signal.poly_price,
            size=signal.shares,
            side="BUY",
            order_type=OrderType.GTC,
        )
        resp = clob.create_and_post_order(order)
        log.info(f"[LIVE] Order placed: {resp}")
        return True
    except Exception as e:
        log.error(f"Order execution failed: {e}")
        return False


def record_open_position(event_id: int, signal: TradeSignal, event_title: str,
                         minute: int, score: str, home_team: str, away_team: str,
                         condition_id: str = "", neg_risk: bool = True,
                         league: str = "", liquidity: LiquidityInfo | None = None):
    """Record an open position — same for paper and live."""
    global _session_trades
    _session_trades += 1

    _open_positions[event_id] = {
        "event_title": event_title,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "minute": minute,
        "score_at_entry": score,
        "home_team": home_team,
        "away_team": away_team,
        "market_question": signal.market_question,
        "side": signal.side,
        "token_id": signal.token_id,
        "condition_id": condition_id,
        "neg_risk": neg_risk,
        "poly_price": signal.poly_price,
        "true_prob": signal.true_prob,
        "book_implied": signal.book_implied,
        "edge_pct": signal.edge_pct,
        "stake": signal.stake,
        "shares": signal.shares,
        "profit_if_win": signal.profit_if_win,
        "loss_if_lose": signal.loss_if_lose,
        "expected_value": signal.expected_value,
        "league": league,
        "liquidity_depth": liquidity.total_depth if liquidity else 0,
        "liquidity_spread": liquidity.spread if liquidity else 0,
        "liquidity_available": liquidity.available_shares if liquidity else 0,
        "trade_type": signal.reason.split("]")[0].strip("[") if "[" in signal.reason else "STANDARD",
    }
    save_state()


# ── Result Resolution ───────────────────────────────────────


def _determine_outcome(position: dict, final_home: int, final_away: int) -> str:
    """Determine if our position won or lost based on final score.

    Returns 'WIN', 'LOSS', or 'UNKNOWN'.
    """
    question = position["market_question"].lower()
    side = position["side"]  # "YES" or "NO"
    home = position["home_team"].lower()
    away = position["away_team"].lower()

    # Figure out what the market asks
    is_draw_market = "draw" in question
    is_home_win_market = False
    is_away_win_market = False

    if not is_draw_market:
        # Check which team the "win" market is about
        for word in home.split():
            if len(word) >= 3 and word in question:
                is_home_win_market = True
                break
        if not is_home_win_market:
            for word in away.split():
                if len(word) >= 3 and word in question:
                    is_away_win_market = True
                    break

    # Determine the actual outcome
    if is_draw_market:
        market_outcome_yes = (final_home == final_away)
    elif is_home_win_market:
        market_outcome_yes = (final_home > final_away)
    elif is_away_win_market:
        market_outcome_yes = (final_away > final_home)
    else:
        return "UNKNOWN"

    # Did our side win?
    if side == "YES":
        return "WIN" if market_outcome_yes else "LOSS"
    else:  # NO
        return "WIN" if not market_outcome_yes else "LOSS"


def redeem_winning_position(position: dict, web3_client, mode: str):
    """Redeem settled winning shares to free up capital.

    In paper mode, logs what would be redeemed. In live mode, calls the contract.
    """
    condition_id = position.get("condition_id", "")
    neg_risk = position.get("neg_risk", True)
    shares = position["shares"]
    payout = shares  # winning shares pay $1 each

    if mode == "paper":
        log.info(f"[PAPER] Would redeem {shares} shares -> ${payout:.2f} USDC")
        return

    if not condition_id:
        log.warning("No condition_id stored — cannot auto-redeem. Redeem manually on Polymarket.")
        return

    if web3_client is None:
        log.warning("Web3 client not initialized — cannot auto-redeem. Redeem manually on Polymarket.")
        return

    try:
        receipt = web3_client.redeem_position(
            condition_id=condition_id,
            amounts=[shares],
            neg_risk=neg_risk,
        )
        log.info(f"[LIVE] Redeemed {shares} shares -> ${payout:.2f} USDC | tx: {receipt.transaction_hash}")
    except Exception as e:
        log.error(f"Auto-redeem failed: {e}. Redeem manually on Polymarket.")


def resolve_ended_matches(events: list, web3_client, mode: str, auto_redeem: bool):
    """Check all events for ended matches that have open positions. Resolve them."""
    global _session_pnl, _session_wins, _session_losses

    resolved_ids = []

    for event, _league_key, _league_cfg in events:
        if event.id not in _open_positions:
            continue

        # Check if match has ended
        is_ended = (
            event.ended
            or str(event.period or "").upper() in ("FT", "POST", "VFT", "AET", "PEN")
        )
        if not is_ended:
            continue

        position = _open_positions[event.id]
        final_score = str(event.score or "0-0")
        final_home, final_away = parse_score(final_score)

        outcome = _determine_outcome(position, final_home, final_away)

        if outcome == "WIN":
            pnl = position["profit_if_win"]
            _session_wins += 1
        elif outcome == "LOSS":
            pnl = -position["loss_if_lose"]
            _session_losses += 1
        else:
            log.warning(f"Could not determine outcome for {position['event_title']}")
            continue

        _session_pnl += pnl

        # Print resolution
        print()
        print("*" * 60)
        print(f"  RESULT: {outcome}")
        print(f"  MATCH: {position['event_title']}")
        print(f"  Final Score: {final_score}")
        print(f"  Position: {position['side']} on \"{position['market_question']}\"")
        print(f"  Entry: {position['score_at_entry']} at min {position['minute']}")
        print(f"  P&L: {'+'if pnl >= 0 else ''}{pnl:.2f}")
        print(f"  ---")
        print(f"  Session: {_session_wins}W-{_session_losses}L | "
              f"P&L: {'+'if _session_pnl >= 0 else ''}{_session_pnl:.2f}")
        print("*" * 60)
        print()

        # Auto-redeem winning positions to free up capital
        if outcome == "WIN" and auto_redeem:
            redeem_winning_position(position, web3_client, mode)

        # Log to file
        _log_resolved_trade(position, final_score, outcome, pnl)
        resolved_ids.append(event.id)

    for eid in resolved_ids:
        del _open_positions[eid]

    if resolved_ids:
        save_state()


def _log_resolved_trade(position: dict, final_score: str, outcome: str, pnl: float):
    """Append resolved trade to the trades log."""
    TRADES_DIR.mkdir(exist_ok=True)
    record = {
        "timestamp": position["opened_at"],
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "event": position["event_title"],
        "league": position.get("league", ""),
        "trade_type": position.get("trade_type", "STANDARD"),
        "minute": position["minute"],
        "score_at_entry": position["score_at_entry"],
        "final_score": final_score,
        "market": position["market_question"],
        "side": position["side"],
        "poly_price": position["poly_price"],
        "true_prob": position["true_prob"],
        "book_implied": position["book_implied"],
        "edge_pct": position["edge_pct"],
        "stake": position["stake"],
        "shares": position["shares"],
        "outcome": outcome,
        "pnl": round(pnl, 2),
        "session_pnl": round(_session_pnl, 2),
        "liquidity_depth": position.get("liquidity_depth", 0),
        "liquidity_spread": position.get("liquidity_spread", 0),
    }
    daily_file = _trades_file_for_today()
    with open(daily_file, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── Display ─────────────────────────────────────────────────


def print_signal(signal: TradeSignal, event_title: str, minute: int, score: str, mode: str):
    """Pretty-print a trade signal."""
    tag = mode.upper()
    print()
    print("=" * 60)
    print(f"  [{tag}] MATCH: {event_title}")
    print(f"  MINUTE: {minute} | SCORE: {score}")
    print(f"  SIGNAL: {signal.action}")
    print(f"  REASON: {signal.reason}")
    print("-" * 60)
    if signal.action == "BUY":
        print(f"  Market:     {signal.market_question}")
        print(f"  Side:       {signal.side}")
        print(f"  True Prob:  {signal.true_prob:.1%}")
        print(f"  Poly Price: {signal.poly_price:.4f}")
        if signal.book_implied > 0:
            print(f"  Book Impl:  {signal.book_implied:.1%}")
        print(f"  Edge:       {signal.edge_pct:+.1f}%")
        print(f"  Kelly 25%:  {signal.kelly_fraction:.1f}%")
        print(f"  Stake:      ${signal.stake:.2f}")
        print(f"  Shares:     {signal.shares}")
        print(f"  Win -> +${signal.profit_if_win:.2f}")
        print(f"  Lose -> -${signal.loss_if_lose:.2f}")
        print(f"  EV:         ${signal.expected_value:.2f}")
    if signal.adjustments:
        print("-" * 60)
        print("  Adjustments:")
        for adj in signal.adjustments:
            print(f"    {adj}")
    print("=" * 60)
    print()


def print_status(live_count: int, total: int, mode: str, poll_sec: int, bankroll: float):
    """Print periodic status line."""
    now = datetime.now().strftime("%H:%M:%S")
    tag = mode.upper()
    open_count = len(_open_positions)
    pnl_str = f"{'+'if _session_pnl >= 0 else ''}{_session_pnl:.2f}"
    log.info(
        f"[{now}] [{tag}] {live_count} live / {total} total | "
        f"Open: {open_count} | Resolved: {_session_wins}W-{_session_losses}L | "
        f"P&L: {pnl_str} | Bankroll: ${bankroll:,.0f} | "
        f"Next poll {poll_sec}s"
    )


# ── Main Loop ───────────────────────────────────────────────


def _get_poly_implied(markets: list[dict], home_team: str = "", away_team: str = "") -> tuple[float, float, float]:
    """Extract implied probabilities from Polymarket prices.

    Returns (home_win_prob, draw_prob, away_win_prob) from moneyline markets.
    Matches team names to the correct market instead of assuming order.
    """
    home_prob = 0.0
    draw_prob = 0.0
    away_prob = 0.0
    unmatched = []

    for m in markets:
        q = (m.get("question") or "").lower()
        prices = m.get("outcome_prices", [])
        if not prices:
            continue
        yes_price = float(prices[0]) if prices else 0
        if "draw" in q:
            draw_prob = yes_price
        elif home_team and any(w in q for w in home_team.lower().split() if len(w) >= 3):
            home_prob = yes_price
        elif away_team and any(w in q for w in away_team.lower().split() if len(w) >= 3):
            away_prob = yes_price
        else:
            unmatched.append(yes_price)

    # Fallback if team names didn't match — use higher price as "favorite"
    if home_prob == 0 and away_prob == 0 and len(unmatched) >= 2:
        home_prob = unmatched[0]
        away_prob = unmatched[1]
    elif home_prob == 0 and unmatched:
        home_prob = unmatched[0]
    elif away_prob == 0 and unmatched:
        away_prob = unmatched[0]
    return home_prob, draw_prob, away_prob


def _is_tier_mismatch_from_poly(markets: list[dict], config_tm: dict) -> tuple[bool, float, float]:
    """Check if match is a tier mismatch using Polymarket prices.

    Returns (is_mismatch, favorite_prob, underdog_prob).
    Ignores settled/near-settled markets (prices at 0% or 95%+) since those
    aren't mismatches — they're just matches where the result is already decided.
    """
    if not config_tm.get("enabled", False):
        return False, 0, 0
    home_prob, _, away_prob = _get_poly_implied(markets)
    if home_prob <= 0 and away_prob <= 0:
        return False, 0, 0
    fav_prob = max(home_prob, away_prob)
    underdog_prob = min(home_prob, away_prob)
    # Settled/in-game filter
    if fav_prob >= 0.95 or underdog_prob < 0.02:
        return False, 0, 0
    # Both conditions must be true: favorite strong enough AND underdog weak enough
    min_fav = config_tm.get("min_favorite_prob", 0.65)
    max_underdog = config_tm.get("max_underdog_prob", 0.15)
    return fav_prob >= min_fav and underdog_prob <= max_underdog, fav_prob, underdog_prob


def _get_bankroll(config: dict, mode: str, web3_client) -> float:
    """Get the real bankroll. Live = wallet USDC balance. Paper = config value."""
    if mode == "live" and web3_client is not None:
        try:
            balance = web3_client.get_usdc_balance()
            # USDC has 6 decimals on Polygon
            real_balance = balance / 1_000_000
            log.info(f"Wallet USDC balance: ${real_balance:,.2f}")
            return real_balance
        except Exception as e:
            log.warning(f"Could not fetch wallet balance: {e} — using config value")
    return float(config["bankroll"])


# Scheduled pre-match bets: event_id → {bet_at: datetime, odds: ..., ...}
_pre_match_scheduled: dict[int, dict] = {}


def _scan_pre_match_mismatches(
    events: list, config: dict, tier_mismatch_cfg: dict,
    risk: dict, bankroll: float, clob, clob_ro: PolymarketReadOnlyClobClient, mode: str,
):
    """Scan upcoming matches for pre-match tier mismatch bets.

    Two phases:
    1. SCAN: find all upcoming mismatches across all leagues, schedule them
    2. EXECUTE: when a scheduled match is within bet_minutes_before of kickoff, place the bet
       (better prices than betting hours early or waiting until in-game)
    """
    if not tier_mismatch_cfg.get("pre_match"):
        return

    from engine import _find_market

    min_edge = tier_mismatch_cfg.get("pre_match_min_edge_pct", 2.0)
    bet_minutes_before = tier_mismatch_cfg.get("pre_match_bet_minutes_before", 30)
    now = datetime.now(timezone.utc)

    # ── Phase 1: SCAN and schedule new mismatches ──
    for event, league_key, league_cfg in events:
        if event.live or event.ended:
            continue
        if event.id in _evaluated_event_ids or event.id in _open_positions:
            continue
        if event.id in _pre_match_scheduled:
            continue

        start_time = event.start_time
        if start_time is None:
            continue
        if isinstance(start_time, str):
            try:
                start_time = datetime.fromisoformat(start_time)
            except ValueError:
                continue
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)

        # Skip matches already started, already ended, or too far out
        bet_at_time = start_time - __import__("datetime").timedelta(minutes=bet_minutes_before)
        hours_until = (start_time - now).total_seconds() / 3600
        if bet_at_time < now:
            # Bet window already passed — don't schedule
            continue
        # Only schedule matches kicking off today (same UTC date)
        if start_time.date() != now.date():
            continue

        home_team, away_team = parse_teams_from_title(event.title or "")
        if not home_team or not away_team:
            continue

        # Check mismatch using Polymarket's own prices
        markets = markets_to_dicts(event.markets)
        is_mismatch, fav_prob, underdog_prob = _is_tier_mismatch_from_poly(markets, tier_mismatch_cfg)
        if not is_mismatch:
            continue

        # Determine favorite and underdog from Polymarket prices
        home_prob, _, away_prob = _get_poly_implied(markets, home_team, away_team)
        fav_is_home = home_prob > away_prob
        underdog_team = away_team if fav_is_home else home_team
        fav_team = home_team if fav_is_home else away_team

        _pre_match_scheduled[event.id] = {
            "event_title": event.title,
            "kickoff": start_time,
            "bet_at": start_time - __import__("datetime").timedelta(minutes=bet_minutes_before),
            "league_cfg": league_cfg,
            "home_team": home_team,
            "away_team": away_team,
            "fav_team": fav_team,
            "underdog_team": underdog_team,
            "fav_is_home": fav_is_home,
            "fav_prob": fav_prob,
        }
        hours_str = f"{hours_until:.1f}h" if hours_until >= 1 else f"{hours_until * 60:.0f}min"
        bet_time_str = _pre_match_scheduled[event.id]["bet_at"].strftime("%H:%M")
        log.info(f"[PRE-MATCH] Scheduled: {event.title} | {fav_team} {fav_prob:.0%} fav | "
                 f"Kickoff in {hours_str} | Will bet at {bet_time_str} UTC")

    # ── Phase 2: EXECUTE scheduled bets whose time has come ──
    executed_ids = []
    for event_id, sched in _pre_match_scheduled.items():
        if now < sched["bet_at"]:
            continue  # not time yet

        if len(_open_positions) >= risk["max_concurrent_positions"]:
            log.info(f"Max concurrent positions — deferring {sched['event_title']}")
            continue

        # Find the event again to get fresh Polymarket prices at bet time
        found_event = None
        for ev, _lk, _lc in events:
            if ev.id == event_id:
                found_event = ev
                break
        if found_event is None:
            executed_ids.append(event_id)
            continue

        markets = markets_to_dicts(found_event.markets)

        # Re-check mismatch with fresh prices
        fav_is_home = sched["fav_is_home"]
        home_prob, _, away_prob = _get_poly_implied(markets, sched["home_team"], sched["away_team"])
        underdog_win_prob = away_prob if fav_is_home else home_prob
        underdog_not_win_prob = 1.0 - underdog_win_prob

        underdog_market = _find_market(markets, sched["underdog_team"], "win")
        if underdog_market is None:
            executed_ids.append(event_id)
            continue

        prices = underdog_market.get("outcome_prices", [])
        token_ids = underdog_market.get("token_ids", [])
        if len(prices) < 2 or len(token_ids) < 2:
            executed_ids.append(event_id)
            continue

        no_price = prices[1]
        no_token = token_ids[1]
        if no_price <= 0 or no_price >= 1:
            executed_ids.append(event_id)
            continue

        # Check edge
        edge = underdog_not_win_prob - no_price
        if edge < min_edge / 100.0:
            log.info(f"[PRE-MATCH] No edge for {sched['event_title']}: "
                     f"true={underdog_not_win_prob:.1%} poly={no_price:.4f} edge={edge:.1%}")
            executed_ids.append(event_id)
            continue

        # Kelly sizing
        kelly_frac = risk.get("kelly_fraction", 0.25)
        max_stake = risk.get("max_single_stake", 1000)
        kelly_full = (underdog_not_win_prob - no_price) / (1.0 - no_price)
        kelly_sized = kelly_full * kelly_frac
        stake = min(bankroll * kelly_sized, max_stake)
        if stake < 1:
            executed_ids.append(event_id)
            continue

        shares = int(stake / no_price)
        profit_if_win = round(shares * (1.0 - no_price), 2)
        loss_if_lose = round(shares * no_price, 2)
        ev = round(underdog_not_win_prob * profit_if_win - underdog_win_prob * loss_if_lose, 2)

        signal = TradeSignal(
            action="BUY",
            reason=f"[PRE-MATCH] {sched['fav_team']} ({sched['fav_prob']:.0%}) vs {sched['underdog_team']} ({underdog_win_prob:.0%})",
            market_question=underdog_market["question"],
            side="NO",
            token_id=no_token,
            true_prob=underdog_not_win_prob,
            poly_price=no_price,
            book_implied=underdog_not_win_prob,
            edge_pct=edge * 100,
            kelly_fraction=kelly_sized * 100,
            stake=round(stake, 2),
            shares=shares,
            profit_if_win=profit_if_win,
            loss_if_lose=loss_if_lose,
            expected_value=ev,
        )

        # Check liquidity before placing
        yes_token = token_ids[0] if token_ids else ""
        liq = check_liquidity(clob_ro, no_token, "NO", shares, no_price, complement_token_id=yes_token)
        if not liq.sufficient:
            log.info(f"[PRE-MATCH] Skipping {sched['event_title']}: insufficient liquidity "
                     f"({liq.available_shares} available vs {shares} needed, "
                     f"depth=${liq.total_depth:.0f}, spread={liq.spread:.3f})")
            executed_ids.append(event_id)
            continue

        mins_to_kick = max(0, (sched["kickoff"] - now).total_seconds() / 60)
        print_signal(signal, f"{sched['event_title']} ({mins_to_kick:.0f}min to kickoff)", 0, "pre-match", mode)
        log.info(f"  Liquidity: {liq.available_shares} shares @ {liq.best_price:.3f} | "
                 f"depth=${liq.total_depth:.0f} | spread={liq.spread:.3f} | {liq.levels} levels")

        cond_id = underdog_market.get("condition_id", "")
        neg_risk = underdog_market.get("neg_risk", True)

        placed = place_order(signal, clob, mode)
        if placed:
            record_open_position(
                event_id, signal, sched["event_title"],
                0, "pre-match", sched["home_team"], sched["away_team"],
                condition_id=cond_id, neg_risk=neg_risk,
                league=sched["league_cfg"].get("name", ""), liquidity=liq,
            )
        executed_ids.append(event_id)

    for eid in executed_ids:
        _evaluated_event_ids.add(eid)
        _pre_match_scheduled.pop(eid, None)


def run_loop(config: dict, mode: str):
    global _session_pnl

    # Paper and live data are completely separate
    _init_data_paths(mode)
    log.info(f"Data directory: {TRADES_DIR}")

    from polymarket_apis import PolymarketWeb3Client

    gamma = PolymarketGammaClient()
    leagues = config["leagues"]
    risk = config["risk"]
    poll_sec = config["strategy"]["poll_interval_sec"]
    min_minute = config["strategy"]["min_minute"]
    pre_fetch_minute = config["strategy"].get("pre_fetch_minute", 78)
    tier_mismatch_cfg = config.get("tier_mismatch", {})
    auto_redeem = config.get("auto_redeem", True)

    # Initialize clients for live trading
    clob = None
    web3_client = None
    if mode == "live":
        pk = config.get("polymarket_private_key", "")
        funder = config.get("polymarket_funder", "")
        if pk.startswith("YOUR_") or not pk:
            log.error("Polymarket private key not configured — cannot run in live mode")
            sys.exit(1)
        clob = PolymarketClobClient(
            private_key=pk,
            chain_id=137,
            funder=funder if funder and not funder.startswith("YOUR_") else None,
        )
        web3_client = PolymarketWeb3Client(private_key=pk, chain_id=137)
        log.info("CLOB + Web3 clients initialized for LIVE trading")

    # Read-only CLOB client for orderbook checks (works in both modes)
    clob_ro = PolymarketReadOnlyClobClient()

    # Get real bankroll: live = wallet balance, paper = config value
    bankroll = _get_bankroll(config, mode, web3_client)

    tag = mode.upper()
    banner = f"""
##########################################################
#                                                        #
#   ____       _        ____        _                    #
#  |  _ \\ ___ | |_   _ | __ )  ___ | |_                 #
#  | |_) / _ \\| | | | ||  _ \\ / _ \\| __|                #
#  |  __/ (_) | | |_| || |_) | (_) | |_                 #
#  |_|   \\___/|_|\\__, ||____/ \\___/ \\__|                #
#                |___/                                   #
#                                                        #
#   Polymarket Football Trading Bot  {__version__:<19s}#
#   Mode: {tag:<44s}#
#                                                        #
##########################################################
"""
    sys.stdout.write(banner)
    sys.stdout.flush()
    log.info(f"Bankroll: ${bankroll:,.2f}")
    log.info(f"Monitoring {len(leagues)} leagues | Trade from minute {min_minute}+")
    if tier_mismatch_cfg.get("enabled"):
        mins = tier_mismatch_cfg.get("pre_match_bet_minutes_before", 30)
        log.info(f"Tier mismatch: ON (pre-match only) | Favorite >= {tier_mismatch_cfg['min_favorite_prob']:.0%} | Bet {mins}min before kickoff")
    log.info(f"Risk: max_stake=${risk['max_single_stake']} | min_edge={risk['min_edge_pct']}% | kelly={risk['kelly_fraction']*100:.0f}%")
    log.info(f"Auto-redeem: {'ON' if auto_redeem else 'OFF'}")

    # Resume state from previous run (open positions, P&L, etc.)
    load_state()
    print()

    prefetched_stats: dict[int, tuple] = {}
    _last_scan_date: str = ""  # track which day we've scanned

    while True:
        try:
            events = discover_football_events(gamma, leagues)
            live_count = 0
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            # ── 0. Daily scan summary (once per day) ──
            if today_str != _last_scan_date:
                _last_scan_date = today_str
                _pre_match_scheduled.clear()  # reset for new day

                print()
                log.info(f"=== DAILY SCAN: {today_str} ===")
                by_league: dict[str, list] = {}
                for ev, lk, lc in events:
                    by_league.setdefault(lc.get("name", lk), []).append(ev)
                for league_name, league_events in sorted(by_league.items()):
                    today_matches = []
                    for ev in league_events:
                        st = ev.start_time
                        if st is None:
                            continue
                        if isinstance(st, str):
                            try:
                                st = datetime.fromisoformat(st)
                            except ValueError:
                                continue
                        if st.tzinfo is None:
                            st = st.replace(tzinfo=timezone.utc)
                        if st.date() == datetime.now(timezone.utc).date():
                            status = "LIVE" if ev.live else ("ENDED" if ev.period in ("FT", "POST", "VFT") else f"KO {st.strftime('%H:%M')} UTC")
                            today_matches.append(f"{ev.title} [{status}]")
                    if today_matches:
                        log.info(f"  {league_name}: {len(today_matches)} matches")
                        for m in today_matches:
                            log.info(f"    {m}")
                log.info(f"=== END SCAN ({sum(len(v) for v in by_league.values())} total across {len(by_league)} leagues) ===")
                print()

            # ── 1. Resolve ended matches with open positions ──
            # Fetch open position events directly by ID (they may have dropped
            # from the active/not-closed filter once the match ended)
            if _open_positions:
                open_ids = [str(eid) for eid in _open_positions.keys()]
                try:
                    resolved_events = gamma.get_events(event_ids=open_ids, limit=len(open_ids))
                    # Add them to the events list with a dummy league (resolution doesn't need it)
                    existing_ids = {e.id for e, _, _ in events}
                    for re in resolved_events:
                        if re.id not in existing_ids:
                            events.append((re, "_resolved", {}))
                except Exception:
                    pass  # fall back to normal events list

            resolve_ended_matches(events, web3_client, mode, auto_redeem)

            # Update effective bankroll: subtract open stakes, add resolved P&L
            open_stakes = sum(p["stake"] for p in _open_positions.values())
            effective_bankroll = bankroll + _session_pnl - open_stakes

            # ── 2. Pre-match tier mismatch scan ──
            _scan_pre_match_mismatches(
                events, config, tier_mismatch_cfg,
                risk, effective_bankroll, clob, clob_ro, mode,
            )

            # ── 2. Scan live matches for new trades ──
            live_status_lines = []
            for event, league_key, league_cfg in events:
                if not event.live:
                    continue
                live_count += 1

                elapsed = event.elapsed
                if not elapsed or str(elapsed).strip() == "":
                    live_status_lines.append(f"  LIVE: {(event.title or '')[:40]} | {event.score} | period={event.period} (no minute)")
                    continue
                try:
                    minute = int(elapsed)
                except (ValueError, TypeError):
                    continue
                score_str = str(event.score or "0-0")
                home_goals, away_goals = parse_score(score_str)
                home_team, away_team = parse_teams_from_title(event.title or "")

                live_status_lines.append(f"  LIVE: {(event.title or '')[:40]} | {score_str} | min {minute}")

                # Skip if we already have a position or already evaluated
                if event.id in _evaluated_event_ids or event.id in _open_positions:
                    continue

                # Check daily loss limit
                if _session_pnl <= -risk["max_daily_loss"]:
                    log.warning(f"Daily loss limit reached (${_session_pnl:.2f}) — not taking new trades")
                    break

                # Build markets list — needed for mismatch detection and trading
                markets = markets_to_dicts(event.markets)
                if not markets:
                    log.warning(f"[{event.title}] No moneyline markets found")
                    _evaluated_event_ids.add(event.id)
                    continue

                # Live matches = standard rules only (minute 80+)
                # Tier mismatch is pre-match only — once started, Polymarket
                # data is too slow/unreliable to verify clean conditions
                # (no goals, no red cards, etc.)
                if minute < min_minute:
                    if minute >= pre_fetch_minute:
                        log.info(f"[{event.title}] Min {minute} | {score_str} | waiting for {min_minute}...")
                    continue

                # Check max concurrent positions
                if len(_open_positions) >= risk["max_concurrent_positions"]:
                    log.info(f"Max concurrent positions ({risk['max_concurrent_positions']}) reached — skipping")
                    continue

                # Run decision engine — no external APIs, uses score + clock + Polymarket prices
                signal = evaluate(
                    home_team=home_team,
                    away_team=away_team,
                    home_goals=home_goals,
                    away_goals=away_goals,
                    minute=minute,
                    stats=MatchStats(),
                    odds=BookmakerOdds(),
                    markets=markets,
                    bankroll=effective_bankroll,
                    config_risk=risk,
                )

                print_signal(signal, event.title, minute, score_str, mode)

                if signal.action == "BUY":
                    # Find condition_id from the matched market
                    cond_id = ""
                    neg_risk = True
                    for m in markets:
                        if m["question"] == signal.market_question:
                            cond_id = m.get("condition_id", "")
                            neg_risk = m.get("neg_risk", True)
                            break

                    # Check liquidity — find complement token for cross-book check
                    comp_token = ""
                    for m in markets:
                        if m["question"] == signal.market_question:
                            tids = m.get("token_ids", [])
                            if signal.side == "YES" and len(tids) >= 2:
                                comp_token = tids[1]
                            elif signal.side == "NO" and len(tids) >= 1:
                                comp_token = tids[0]
                            break
                    liq = check_liquidity(clob_ro, signal.token_id, signal.side, signal.shares, signal.poly_price, complement_token_id=comp_token)
                    if not liq.sufficient:
                        # Retry up to 10 times over ~10 seconds — orderbook might refill
                        for retry in range(10):
                            time.sleep(1)
                            liq = check_liquidity(clob_ro, signal.token_id, signal.side, signal.shares, signal.poly_price, complement_token_id=comp_token)
                            if liq.sufficient:
                                log.info(f"  Retry {retry+1}/10 found liquidity: {liq.available_shares} shares")
                                break
                        else:
                            log.info(f"  Skipping: insufficient liquidity after 10 retries "
                                     f"({liq.available_shares} available vs {signal.shares} needed, "
                                     f"depth=${liq.total_depth:.0f})")
                            _evaluated_event_ids.add(event.id)
                            continue

                    # Reduce size to available liquidity if partial fill
                    if liq.available_shares > 0 and liq.available_shares < signal.shares:
                        ratio = liq.available_shares / signal.shares
                        signal.shares = liq.available_shares
                        signal.stake = round(signal.shares * signal.poly_price, 2)
                        signal.profit_if_win = round(signal.shares * (1.0 - signal.poly_price), 2)
                        signal.loss_if_lose = round(signal.shares * signal.poly_price, 2)
                        signal.expected_value = round(
                            signal.true_prob * signal.profit_if_win -
                            (1.0 - signal.true_prob) * signal.loss_if_lose, 2)
                        log.info(f"  Partial fill: reduced to {signal.shares} shares "
                                 f"(${signal.stake:.0f}, {ratio:.0%} of target)")

                    log.info(f"  Liquidity: {liq.available_shares} shares | "
                             f"depth=${liq.total_depth:.0f} | spread={liq.spread:.3f} | {liq.levels} levels")

                    placed = place_order(signal, clob, mode)
                    if placed:
                        record_open_position(
                            event.id, signal, event.title,
                            minute, score_str, home_team, away_team,
                            condition_id=cond_id, neg_risk=neg_risk,
                            league=league_cfg.get("name", league_key), liquidity=liq,
                        )
                    _evaluated_event_ids.add(event.id)
                else:
                    _evaluated_event_ids.add(event.id)

            # ── 3. Status ──
            print_status(live_count, len(events), mode, poll_sec, effective_bankroll)

            # Print live match status
            for line in live_status_lines:
                log.info(line)

            # Print open positions with current match state + live valuation
            if _open_positions:
                print("-" * 60)
                total_cost = 0.0
                total_value = 0.0
                print(f"  OPEN BETS ({len(_open_positions)}):")
                for eid, pos in _open_positions.items():
                    # Find current score and live price for this event
                    current_score = pos["score_at_entry"]
                    current_min = pos["minute"]
                    current_price = pos["poly_price"]  # fallback to entry price
                    for ev, _lk, _lc in events:
                        if ev.id == eid:
                            current_score = str(ev.score or current_score)
                            if ev.elapsed and str(ev.elapsed).strip():
                                try:
                                    current_min = int(ev.elapsed)
                                except (ValueError, TypeError):
                                    pass
                            # Get current price from live market data
                            import json as _json
                            for m in (ev.markets or []):
                                if m.sports_market_type != "moneyline":
                                    continue
                                mq = (m.question or "").lower()
                                pq = (pos["market_question"] or "").lower()
                                # Match by checking if key words overlap
                                if any(w in mq for w in pq.split() if len(w) >= 4):
                                    p = m.outcome_prices
                                    if isinstance(p, str):
                                        p = _json.loads(p)
                                    if p:
                                        if pos["side"] == "YES":
                                            current_price = float(p[0])
                                        elif len(p) > 1:
                                            current_price = float(p[1])
                                    break
                            break

                    cost = pos["shares"] * pos["poly_price"]
                    value = pos["shares"] * current_price
                    unrealized = value - cost
                    total_cost += cost
                    total_value += value

                    # Color indicator
                    pnl_indicator = "+" if unrealized >= 0 else ""

                    print(f"    {pos['event_title']}")
                    print(f"      Now: {current_score} min {current_min} | {pos['side']} on \"{pos['market_question']}\"")
                    print(f"      Entry: {pos['shares']} shares @ {pos['poly_price']:.3f} = ${cost:.0f} | Now @ {current_price:.3f} = ${value:.0f} | P&L: {pnl_indicator}${unrealized:.0f}")

                total_unrealized = total_value - total_cost
                pnl_ind = "+" if total_unrealized >= 0 else ""
                print(f"  TOTAL: Cost ${total_cost:.0f} | Value ${total_value:.0f} | Unrealized {pnl_ind}${total_unrealized:.0f}")
                print("-" * 60)

            # Portfolio summary — realized P&L from our trades, unrealized from live Polymarket prices
            open_stakes = sum(p["stake"] for p in _open_positions.values())
            total_unrealized = (total_value - total_cost) if _open_positions else 0
            total_pnl = _session_pnl + total_unrealized

            # In live mode, try to get real wallet balance for accurate "Available"
            wallet_balance = None
            if mode == "live" and web3_client is not None:
                try:
                    wallet_balance = web3_client.get_usdc_balance() / 1_000_000
                except Exception:
                    pass

            print("=" * 60)
            print(f"  PORTFOLIO")
            print(f"    Realized:    ${_session_pnl:+,.2f} ({_session_wins}W-{_session_losses}L, {_session_trades} trades)")
            print(f"    Unrealized:  ${total_unrealized:+,.0f} ({len(_open_positions)} open, ${open_stakes:,.0f} staked)")
            print(f"    Total P&L:   ${total_pnl:+,.2f}")
            if wallet_balance is not None:
                print(f"    Wallet:      ${wallet_balance:,.2f} USDC (live)")
            else:
                print(f"    Available:   ${effective_bankroll:,.0f} (tracked)")
            print("=" * 60)

            # Print scheduled pre-match bets
            if _pre_match_scheduled:
                for eid, sched in _pre_match_scheduled.items():
                    mins_until_bet = max(0, (sched["bet_at"] - datetime.now(timezone.utc)).total_seconds() / 60)
                    log.info(f"  SCHED: {sched['event_title']} | {sched['fav_team']} {sched['fav_prob']:.0%} fav | "
                             f"Bet in {mins_until_bet:.0f}min")

        except KeyboardInterrupt:
            save_state()
            print()
            print("=" * 60)
            print("  Bot stopped. State saved to data/state.json")
            print(f"  Session: {_session_trades} trades | "
                  f"{_session_wins}W-{_session_losses}L | "
                  f"P&L: {'+'if _session_pnl >= 0 else ''}{_session_pnl:.2f}")
            if _open_positions:
                print(f"  {len(_open_positions)} open positions — will resume on next start")
                for pos in _open_positions.values():
                    print(f"    {pos['event_title']} | {pos['side']} \"{pos['market_question']}\" | ${pos['stake']:.0f}")
            print("=" * 60)
            break
        except Exception as e:
            log.error(f"Error in main loop: {e}", exc_info=True)

        time.sleep(poll_sec)


# ── Entry Point ─────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Polymarket Football Trading Bot")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--live", action="store_true", help="Live trading — real money")
    group.add_argument("--paper", action="store_true", help="Paper trading — simulated, state persists")
    group.add_argument("--ephemeral", action="store_true", help="Ephemeral — throwaway run, wiped on start")
    args = parser.parse_args()

    config = load_config()

    if args.live:
        mode = "live"
    elif args.ephemeral:
        import shutil
        ephemeral_dir = BASE_DIR / "ephemeral"
        if ephemeral_dir.exists():
            shutil.rmtree(ephemeral_dir)
        ephemeral_dir.mkdir(parents=True)
        mode = "ephemeral"
    else:
        mode = "paper"

    if mode == "live":
        print()
        print("  *** LIVE TRADING MODE ***")
        print("  Real money will be used. Press Ctrl+C to stop.")
        print()
        confirm = input("  Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("  Aborted.")
            sys.exit(0)

    run_loop(config, mode)


if __name__ == "__main__":
    main()
