"""
NexGame Lite — Data Models
Kage Software · 2026

All shared dataclasses. Precision conventions (LOCKED):
    Pitcher rate stats  → 0.00  (ERA 4.25, WHIP 1.18)
    Batter rate stats   → 0.000 (OBP .312, SLG .445, BABIP .298)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ── Enums ─────────────────────────────────────────────────────────────
class Sport(Enum):
    MLB = "MLB"
    NBA = "NBA"
    WNBA = "WNBA"
    CS2 = "CS2"


class GameStatus(Enum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    FINAL = "final"
    POSTPONED = "postponed"
    TBD = "tbd"


class InjuryStatus(Enum):
    ACTIVE = "active"
    PROBABLE = "probable"          # 85% plays / 15% out
    QUESTIONABLE = "questionable"  # 60% plays / 40% out
    OUT = "out"                    # zeroed, stays on roster
    IR = "ir"                      # zeroed, stays on roster


# ── Player ────────────────────────────────────────────────────────────
@dataclass
class PlayerStats:
    player_id: str
    name: str
    sport: Sport
    injury_status: InjuryStatus = InjuryStatus.ACTIVE
    games_played: int = 0
    data_source: str = "recent"    # 'recent' | 'career' | 'team_avg'

    # MLB pitcher — 0.00 precision
    is_pitcher: bool = False
    is_starter: bool = False
    era: float = 0.00
    whip: float = 0.00
    k_per_9: float = 0.00
    bb_per_9: float = 0.00
    hr_per_9: float = 0.00
    rest_days: int = 0

    # MLB batter — 0.000 precision
    obp: float = 0.000
    slg: float = 0.000
    babip: float = 0.000
    lineup_spot: int = 0           # 1-9, 0 = not in lineup

    # NBA — points per game etc.
    ppg: float = 0.0
    apg: float = 0.0
    rpg: float = 0.0
    usage_rate: float = 0.0
    true_shooting: float = 0.000
    minutes_proj: float = 0.0
    is_starter_nba: bool = False


# ── Team ──────────────────────────────────────────────────────────────
@dataclass
class TeamData:
    team_id: str
    name: str
    abbrev: str
    sport: Sport
    # Roster — players are never removed, only weighted (LOCKED)
    roster: list = field(default_factory=list)
    confirmed_starter: Optional[PlayerStats] = None   # MLB starting pitcher

    # MLB fallbacks — rotation avg (starters ONLY, bullpen excluded)
    rotation_avg_era: float = 0.00
    rotation_avg_whip: float = 0.00
    rotation_avg_k9: float = 0.00
    # Bullpen — always separate, always active (LOCKED)
    bullpen_era: float = 0.00
    bullpen_whip: float = 0.00
    team_obp: float = 0.000
    team_slg: float = 0.000

    # NBA fallbacks
    team_ortg: float = 0.0
    team_drtg: float = 0.0
    team_pace: float = 0.0

    # CS2 — round win% is the core strength signal (no advanced-stats
    # or player-props endpoint exists on the CS2 API; see provider).
    # Derived from the team's own finished maps within the SAME
    # tournament as the match being predicted (verified-available
    # scope; cross-tournament history is a future upgrade).
    cs2_round_win_pct: float = 0.500
    cs2_maps_sample: int = 0        # maps the rating was derived from


# ── Game ──────────────────────────────────────────────────────────────
@dataclass
class GameContext:
    game_id: str
    sport: Sport
    status: GameStatus
    home_team: TeamData
    away_team: TeamData
    game_date: str
    game_time: str
    venue: str = ""
    # live state (fed by provider)
    home_score_live: int = 0
    away_score_live: int = 0
    period_live: str = ""          # "Top 5" / "Q3" etc.
    # MLB context
    park_factor: float = 1.00
    weather_wind_mph: float = 0.0
    weather_temp_f: float = 72.0
    # NBA context
    back_to_back_home: bool = False
    back_to_back_away: bool = False
    # CS2 context — series length (1/3/5). home_team/away_team map
    # onto CS2's team1/team2 (CS2 has no home/away convention).
    best_of: int = 3


# ── Simulation output ─────────────────────────────────────────────────
@dataclass
class IterationResult:
    home_score: int
    away_score: int
    winner: str                    # 'home' | 'away'
    player_stats: dict             # player_id -> {stat: value}
    periods: list                  # per-inning / per-quarter scores


@dataclass
class SimulationOutput:
    game_id: str
    sport: Sport
    home_team: str
    away_team: str
    home_win_pct: float
    away_win_pct: float
    # trimmed 95% window (LOCKED: trim 2.5% each tail)
    score_low_home: int
    score_med_home: int
    score_high_home: int
    score_low_away: int
    score_med_away: int
    score_high_away: int
    player_projections: dict       # player_id -> {name, team, stat: mean}
    confidence: str                # margin confidence: 'high' | 'medium' | 'high_variance'
    simulations_run: int
    generated_at: str
    win_confidence: str = "toss_up" # winner confidence: 'strong_lean' | 'lean' | 'toss_up'
    # MLB only — {'home': {name, era, whip, k_per_9, confirmed},
    #             'away': {...}}. Empty dict for NBA. Default keeps old
    # cached payloads (predicted before this field existed)
    # deserializable via SimulationOutput(**payload).
    pitching_matchup: dict = field(default_factory=dict)


# ── Settling ──────────────────────────────────────────────────────────
@dataclass
class PlayerSettleResult:
    player_id: str
    name: str
    metric: str
    projected: float
    actual: float
    band_low: float
    band_high: float
    correct: bool
    direction: str                 # 'correct' | 'over' | 'under'


@dataclass
class GameSettleResult:
    game_id: str
    sport: str
    predicted_winner: str
    actual_winner: str
    win_loss_correct: bool
    actual_home: int
    actual_away: int
    score_range_correct: bool
    player_results: list
    player_accuracy_pct: float
    settled_at: str
