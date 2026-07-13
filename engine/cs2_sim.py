"""
NexGame Lite — CS2 Simulation Engine
Kage Software · 2026

Odds markets for CS2 are MAP-level (Moneyline = series winner, Map
Handicap = series map margin, Total Maps Over/Under), not round-level
(CONFIRMED live 2026-07-13 against BDL's CS2 API docs/market naming).
So unlike MLB (innings) or NBA/WNBA (quarters), this engine does NOT
simulate individual rounds — it simulates each MAP as a single weighted
coin flip (log5), then plays out a best-of-N series (1/3/5) map by map
until one team clinches ceil(best_of/2) map wins.

home_score / away_score on the returned IterationResult = MAPS WON,
not rounds — e.g. a 2-0 or 2-1 series result in a Bo3. No player-level
stats are simulated (Samurai Picks' CS2 plan is moneyline + map
spread/total only, no player props), so player_stats is always {}.
"""

import random
import config
from models import GameContext, IterationResult


def _log5(round_win_a: float, round_win_b: float) -> float:
    """Bill James' log5 formula: win probability for team A given each
    team's own win rate against a league-average opponent. Standard,
    well-understood way to convert two independent strength ratings
    into a single head-to-head probability — appropriate complexity
    for a mid-tier model with no player-level signal to lean on."""
    a, b = round_win_a, round_win_b
    denom = a + b - 2 * a * b
    if denom <= 0:
        return 0.5   # degenerate case (a==b==0 or ==1) — coin flip
    return (a - a * b) / denom


def simulate_cs2_match(context: GameContext,
                       rng: random.Random) -> IterationResult:
    """One full best-of-N series. best_of comes from the real match
    data (1, 3, or 5); if unset, default to 3 (CONFIRMED as the modal
    value across live matches pulled 2026-07-13)."""
    best_of = context.best_of or 3
    maps_to_clinch = (best_of // 2) + 1

    home_rating = (context.home_team.cs2_round_win_pct
                  or config.CS2_LEAGUE_AVG_ROUND_WIN_PCT)
    away_rating = (context.away_team.cs2_round_win_pct
                  or config.CS2_LEAGUE_AVG_ROUND_WIN_PCT)
    p_home_map = _log5(home_rating, away_rating)

    home_maps = away_maps = 0
    map_results = []
    while home_maps < maps_to_clinch and away_maps < maps_to_clinch:
        home_wins_map = rng.random() < p_home_map
        if home_wins_map:
            home_maps += 1
        else:
            away_maps += 1
        map_results.append({
            "map_number": len(map_results) + 1,
            "winner": "home" if home_wins_map else "away",
        })

    winner = "home" if home_maps > away_maps else "away"

    return IterationResult(
        home_score=home_maps,
        away_score=away_maps,
        winner=winner,
        player_stats={},          # no player props for CS2 (LOCKED scope)
        periods=map_results,
    )
