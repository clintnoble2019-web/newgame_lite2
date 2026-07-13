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
not rounds — e.g. a 2-0 or 2-1 series result in a Bo3.

PLAYER PROPS (reinstated 2026-07-13): each player's per-map kills/
deaths/assists/adr/rating/headshot% (hydrated in the provider from
real player_match_stats history) are sampled once per map actually
played in the simulated series, then combined into a MATCH-TOTAL for
that iteration — counting stats (kills/deaths/assists) summed across
the series' maps, rate stats (adr/rating/headshot_pct) averaged. This
matches the real player_match_stats shape used both for hydration and
for settling, so projections and actuals are always the same unit.
"""

import random
import config
from models import GameContext, IterationResult


def _log5(round_win_a: float, round_win_b: float) -> float:
    """Bill James' log5 formula: win probability for team A given each
    team's own win rate against a league-average opponent. Standard,
    well-understood way to convert two independent strength ratings
    into a single head-to-head probability — appropriate complexity
    for a mid-tier model with no round-by-round signal to lean on for
    the team-strength side of things."""
    a, b = round_win_a, round_win_b
    denom = a + b - 2 * a * b
    if denom <= 0:
        return 0.5   # degenerate case (a==b==0 or ==1) — coin flip
    return (a - a * b) / denom


def _sample_map_stats(roster: list, rng: random.Random) -> dict:
    """One map's worth of per-player stats for one team. Counting
    stats sampled from a normal distribution around the hydrated
    per-map average (floored at 0 — a player can't have negative
    kills); rate stats sampled the same way but clamped to a sane
    range. ~30% relative stddev is a reasonable single-map variance
    assumption for a mid-tier model with no round-by-round signal
    feeding the spread — tightenable later once settled data exists
    to calibrate against."""
    out = {}
    for p in roster:
        if p.cs2_maps_sample == 0:
            continue   # no hydrated history — skip rather than fabricate
        kills = max(0.0, rng.gauss(p.cs2_kills_avg, p.cs2_kills_avg * 0.3))
        deaths = max(0.0, rng.gauss(p.cs2_deaths_avg, p.cs2_deaths_avg * 0.3))
        assists = max(0.0, rng.gauss(p.cs2_assists_avg,
                                     p.cs2_assists_avg * 0.35))
        adr = max(0.0, rng.gauss(p.cs2_adr_avg, p.cs2_adr_avg * 0.25))
        rating = max(0.0, rng.gauss(p.cs2_rating_avg,
                                    p.cs2_rating_avg * 0.25))
        hs_pct = min(100.0, max(0.0, rng.gauss(
            p.cs2_headshot_pct_avg, p.cs2_headshot_pct_avg * 0.2)))
        out[p.player_id] = {
            "name": p.name, "kills": kills, "deaths": deaths,
            "assists": assists, "adr": adr, "rating": rating,
            "headshot_pct": hs_pct,
        }
    return out


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
    player_match_totals: dict = {}
    rate_stat_counts: dict = {}

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

        for roster in (context.home_team.roster, context.away_team.roster):
            for pid, stats in _sample_map_stats(roster, rng).items():
                entry = player_match_totals.setdefault(pid, {
                    "name": stats["name"], "kills": 0.0, "deaths": 0.0,
                    "assists": 0.0, "adr": 0.0, "rating": 0.0,
                    "headshot_pct": 0.0,
                })
                entry["kills"] += stats["kills"]
                entry["deaths"] += stats["deaths"]
                entry["assists"] += stats["assists"]
                entry["adr"] += stats["adr"]
                entry["rating"] += stats["rating"]
                entry["headshot_pct"] += stats["headshot_pct"]
                rate_stat_counts[pid] = rate_stat_counts.get(pid, 0) + 1

    for pid, entry in player_match_totals.items():
        n = rate_stat_counts.get(pid, 1)
        entry["adr"] = round(entry["adr"] / n, 1)
        entry["rating"] = round(entry["rating"] / n, 2)
        entry["headshot_pct"] = round(entry["headshot_pct"] / n, 1)
        entry["kills"] = round(entry["kills"], 1)
        entry["deaths"] = round(entry["deaths"], 1)
        entry["assists"] = round(entry["assists"], 1)

    winner = "home" if home_maps > away_maps else "away"

    return IterationResult(
        home_score=home_maps,
        away_score=away_maps,
        winner=winner,
        player_stats=player_match_totals,
        periods=map_results,
    )
