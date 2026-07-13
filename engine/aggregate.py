"""
NexGame Lite — Aggregation Layer
Kage Software · 2026

Runs SIMULATION_RUNS iterations and aggregates (LOCKED):
    - Win % = raw iteration count
    - Score range = trim 2.5% each tail -> 9,500-result window
      (low = 5th pctile, median = 50th, high = 95th)
    - Player totals = mean across all iterations
    - Confidence signal derived from range width
"""

import random
from datetime import datetime, timezone
import config
from models import GameContext, Sport, IterationResult, SimulationOutput
from engine.mlb_sim import simulate_mlb_game
from engine.nba_sim import simulate_nba_game
from engine.cs2_sim import simulate_cs2_match


def _trimmed(scores: list[int]) -> tuple[int, int, int]:
    """Sort, trim TRIM_PCT from each tail, return (low, median, high).
    Trim scales with actual run count (tests may use fewer runs)."""
    s = sorted(scores)
    t = int(len(s) * config.TRIM_PCT)
    window = s[t:-t] if t else s
    return window[0], window[len(window) // 2], window[-1]


def _margin_confidence(range_width: int, sport: Sport) -> str:
    """Confidence in the PROJECTED SCORE MARGIN — how tight or wide the
    trimmed score range is. Says nothing about who wins; a heavily
    favored team can still have a wide margin range (could be a close
    win or a blowout)."""
    if sport == Sport.MLB:
        bands = config.CONFIDENCE_BANDS_MLB
    elif sport == Sport.CS2:
        bands = config.CONFIDENCE_BANDS_CS2
    else:
        bands = config.CONFIDENCE_BANDS_NBA   # NBA + WNBA share bands
    if range_width <= bands["high"]:
        return "high"
    if range_width <= bands["medium"]:
        return "medium"
    return "high_variance"


def _win_confidence(home_pct: float) -> str:
    """Confidence in WHO WINS — how lopsided the win probability split
    is. A 86/14 split is a strong lean even if the final margin is
    unpredictable; a 52/48 split is a genuine toss-up even if the
    final margin projects tightly. LOCKED thresholds: >=70% favorite =
    strong lean, >=58% = lean, else toss-up."""
    favorite_pct = max(home_pct, 100 - home_pct)
    if favorite_pct >= 70:
        return "strong_lean"
    if favorite_pct >= 58:
        return "lean"
    return "toss_up"


def run_simulation(context: GameContext,
                   runs: int = None,
                   seed: int = None,
                   progress_cb=None) -> SimulationOutput:
    """Master runner: N iterations -> aggregated SimulationOutput.

    seed: pass for reproducible results (testing).
    progress_cb: optional callable(pct) for dashboard progress bars.
    """
    runs = runs or config.SIMULATION_RUNS
    rng = random.Random(seed)
    if context.sport == Sport.MLB:
        simulate = simulate_mlb_game
    elif context.sport == Sport.CS2:
        simulate = simulate_cs2_match
    else:
        simulate = simulate_nba_game   # NBA + WNBA share this engine

    results: list[IterationResult] = []
    for i in range(runs):
        results.append(simulate(context, rng))
        if progress_cb and i % max(1, runs // 50) == 0:
            progress_cb(i / runs)

    # win %
    home_wins = sum(1 for r in results if r.winner == "home")
    home_pct = round(home_wins / runs * 100, 1)

    # trimmed score ranges
    lo_h, med_h, hi_h = _trimmed([r.home_score for r in results])
    lo_a, med_a, hi_a = _trimmed([r.away_score for r in results])

    # player projections: mean per stat across iterations that included
    # them, PLUS conversion percentages — the % of simulations in which
    # the player recorded at least one hit / at least one RBI. The mean
    # says "how many"; the pct says "how likely at least one" — a .9
    # avg-hits batter might convert a hit in 62% of sims, and that 62%
    # is the number a bettor/viewer actually wants.
    accum: dict = {}
    for r in results:
        for pid, stats in r.player_stats.items():
            entry = accum.setdefault(
                pid, {"name": stats.get("name", pid),
                      # Team tag (added for CS2 2026-07-13): nicknames
                      # aren't unique across teams — two different real
                      # players can both go by e.g. "Shark" — so the
                      # display needs a way to disambiguate. Captured
                      # once like name, never summed.
                      "team": stats.get("_team", ""),
                      "sums": {}, "count": 0,
                      "hit_games": 0, "rbi_games": 0})
            entry["count"] += 1
            for metric, val in stats.items():
                # "name" and any "_"-prefixed metadata (e.g. "_team")
                # are captured above, not summed — summing a string
                # into "sums" would crash.
                if metric == "name" or metric.startswith("_"):
                    continue
                entry["sums"][metric] = entry["sums"].get(metric, 0) + val
            if stats.get("hits", 0) >= 1:
                entry["hit_games"] += 1
            if stats.get("rbis", 0) >= 1:
                entry["rbi_games"] += 1

    projections = {}
    for pid, entry in accum.items():
        if entry["count"] < runs * 0.05:      # ignore rare replacement bats
            continue
        proj = {"name": entry["name"], "team": entry["team"]}
        for metric, total in entry["sums"].items():
            proj[metric] = round(total / runs, 1)
        # conversion pcts only for batters (they carry a 'hits' metric);
        # denominator is ALL runs, matching how the means above are
        # computed — a batter who missed an iteration converted nothing
        # in it. Named *_pct so the settle pipeline never tries to grade
        # them against boxscore actuals (it only settles metrics that
        # appear in the actual stat rows: hits/rbis/strikeouts).
        if "hits" in entry["sums"]:
            proj["hit_pct"] = round(entry["hit_games"] / runs * 100, 1)
            proj["rbi_pct"] = round(entry["rbi_games"] / runs * 100, 1)
        projections[pid] = proj

    # ── pitching matchup (MLB only) — deterministic display block ────
    # Mirrors _resolve_pitcher's decision rule (confirmed starter ->
    # individual stats, else rotation average) but WITHOUT the per-
    # iteration injury roll: this is "who we modeled the matchup on",
    # not one iteration's availability outcome.
    pitching_matchup = {}
    if context.sport == Sport.MLB:
        def _pitcher_block(team):
            sp = team.confirmed_starter
            if sp is not None:
                return {"name": sp.name, "confirmed": True,
                        "era": round(sp.era or config.LEAGUE_AVG_ERA, 2),
                        "whip": round(sp.whip or config.LEAGUE_AVG_WHIP, 2),
                        "k_per_9": round(sp.k_per_9 or 8.50, 2)}
            return {"name": f"{team.name} Rotation Avg", "confirmed": False,
                    "era": round(team.rotation_avg_era or config.LEAGUE_AVG_ERA, 2),
                    "whip": round(team.rotation_avg_whip or config.LEAGUE_AVG_WHIP, 2),
                    "k_per_9": round(team.rotation_avg_k9 or 8.50, 2)}
        pitching_matchup = {
            "home": _pitcher_block(context.home_team),
            "away": _pitcher_block(context.away_team),
        }

 # Margin confidence measures the MARGIN, not each team's individual
    # score spread. Redefined 2026-07-11: previously max(team range
    # widths), which called overlapping ranges (e.g. 3-6 vs 1-3, where
    # anything from a tie to a 5-run blowout is in play) a "tight
    # margin". Now: trimmed width of (home - away) across all
    # iterations — the actual spread of possible victory margins.
    lo_m, med_m, hi_m = _trimmed([r.home_score - r.away_score
                                  for r in results])
    width = hi_m - lo_m

    return SimulationOutput(
        game_id=context.game_id,
        sport=context.sport,
        home_team=context.home_team.name,
        away_team=context.away_team.name,
        home_win_pct=home_pct,
        away_win_pct=round(100 - home_pct, 1),
        score_low_home=lo_h, score_med_home=med_h, score_high_home=hi_h,
        score_low_away=lo_a, score_med_away=med_a, score_high_away=hi_a,
        player_projections=projections,
        confidence=_margin_confidence(width, context.sport),
        win_confidence=_win_confidence(home_pct),
        simulations_run=runs,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        pitching_matchup=pitching_matchup,
    )
