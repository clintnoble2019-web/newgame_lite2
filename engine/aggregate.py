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
    bands = (config.CONFIDENCE_BANDS_MLB if sport == Sport.MLB
             else config.CONFIDENCE_BANDS_NBA)
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
    simulate = (simulate_mlb_game if context.sport == Sport.MLB
                else simulate_nba_game)

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

    # player projections: mean per stat across iterations that included them
    accum: dict = {}
    for r in results:
        for pid, stats in r.player_stats.items():
            entry = accum.setdefault(
                pid, {"name": stats.get("name", pid),
                      "sums": {}, "count": 0})
            entry["count"] += 1
            for metric, val in stats.items():
                if metric == "name":
                    continue
                entry["sums"][metric] = entry["sums"].get(metric, 0) + val

    projections = {}
    for pid, entry in accum.items():
        if entry["count"] < runs * 0.05:      # ignore rare replacement bats
            continue
        proj = {"name": entry["name"]}
        for metric, total in entry["sums"].items():
            proj[metric] = round(total / runs, 1)
        projections[pid] = proj

    width = max(hi_h - lo_h, hi_a - lo_a)

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
    )
