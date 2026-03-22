"""
stats.py — Data types for match statistics and bookmaker odds.
"""

from dataclasses import dataclass


@dataclass
class MatchStats:
    home_shots: int = 0
    away_shots: int = 0
    home_shots_on_target: int = 0
    away_shots_on_target: int = 0
    home_possession: float = 50.0
    away_possession: float = 50.0
    home_corners: int = 0
    away_corners: int = 0
    home_yellow: int = 0
    away_yellow: int = 0
    home_red: int = 0
    away_red: int = 0
    home_xg: float | None = None
    away_xg: float | None = None
    available: bool = False


@dataclass
class BookmakerOdds:
    source: str = ""
    home_win: float = 0.0
    draw: float = 0.0
    away_win: float = 0.0
    home_implied: float = 0.0
    draw_implied: float = 0.0
    away_implied: float = 0.0
    available: bool = False
