"""
NexGame Lite — Settling Pipeline
Kage Software · 2026

THE CREDIBILITY ENGINE. Runs post-game (trigger: status = Final).
Audited by mom. Correctness thresholds (LOCKED):
    Win/Loss      -> binary, predicted winner == actual winner
    Score Range   -> actual final score inside published trimmed range
    Player Totals -> actual within ±20% band of projection
                     misses logged directionally (over/under)
Calibration signal (LOCKED): >20% misses same direction over 15+ games
-> drift flag. Lite stage: LOG ONLY, manual review — no auto-adjust.
"""

from datetime import datetime, timezone
import config
from models import (
    SimulationOutput, PlayerSettleResult, GameSettleResult,
)


def settle_player_total(player_id: str, name: str, metric: str,
                        projected: float, actual: float) -> PlayerSettleResult:
    """±20% band (LOCKED). Example: proj 24.3 pts -> correct 19.4–29.2."""
    band_low = projected * (1 - config.CORRECTNESS_THRESHOLD)
    band_high = projected * (1 + config.CORRECTNESS_THRESHOLD)
    correct = band_low <= actual <= band_high
    if correct:
        direction = "correct"
    elif actual > band_high:
        direction = "over"
    else:
        direction = "under"
    return PlayerSettleResult(
        player_id=player_id, name=name, metric=metric,
        projected=projected, actual=actual,
        band_low=round(band_low, 1), band_high=round(band_high, 1),
        correct=correct, direction=direction,
    )


def settle_game(prediction: SimulationOutput,
                boxscore: dict) -> GameSettleResult:
    """Master settle for one game.
    boxscore = provider.get_final_boxscore() output."""
    actual_home = boxscore["home_score"]
    actual_away = boxscore["away_score"]

    # ── Win/Loss (binary) ────────────────────────────────────────────
    predicted_winner = "home" if prediction.home_win_pct > 50 else "away"
    actual_winner = "home" if actual_home > actual_away else "away"
    win_loss_correct = predicted_winner == actual_winner

  # ── Score range (both teams must land in band) ──────────────────
    home_in = (prediction.score_low_home <= actual_home
               <= prediction.score_high_home)
    away_in = (prediction.score_low_away <= actual_away
               <= prediction.score_high_away)
    score_range_correct = home_in and away_in
    
    # ── Player totals (±20% each) ────────────────────────────────────
    # Direct player_id match first. Fallback to name match (case-
    # insensitive) for the same real person if IDs don't line up —
    # doesn't fully solve the deeper issue (see note below) but catches
    # any ID inconsistency between BallDontLie endpoints.
    #
    # REAL LIMITATION, not a bug: predictions run before real lineups
    # post use an active-roster GUESS at who's likely to bat (see
    # ingest/balldontlie_provider.py::_apply_active_roster_fallback).
    # That guess often won't match the manager's actual real lineup,
    # so many pre-lineup predictions will have naturally low player-
    # level match rates no matter how good the ID/name matching is —
    # the model is projecting the wrong specific humans, not projecting
    # the right ones incorrectly. This should improve substantially
    # once predictions are run closer to game time, after real lineups
    # are posted.
    name_lookup = {
        (proj.get("name", "").strip().lower()): pid
        for pid, proj in prediction.player_projections.items()
        if proj.get("name")
    }

    player_results = []
    for pid, actual_stats in boxscore.get("player_stats", {}).items():
        proj = prediction.player_projections.get(pid)
        if not proj:
            actual_name = actual_stats.get("_name", "").strip().lower()
            fallback_pid = name_lookup.get(actual_name)
            if fallback_pid:
                proj = prediction.player_projections.get(fallback_pid)
        if not proj:
            continue
        for metric, actual_val in actual_stats.items():
            # Skip metadata keys (_name, _team) — underscore prefix
            # marks non-metric fields attached by the provider for
            # name-fallback matching and team-split display.
            if metric.startswith("_"):
                continue
            projected_val = proj.get(metric)
            if projected_val is None or projected_val <= 0:
                continue
            player_results.append(settle_player_total(
                pid, proj.get("name", pid), metric,
                projected_val, actual_val))

    correct_ct = sum(1 for r in player_results if r.correct)
    player_acc = (round(correct_ct / len(player_results) * 100, 1)
                  if player_results else 0.0)

    return GameSettleResult(
        game_id=prediction.game_id,
        sport=prediction.sport.value,
        predicted_winner=predicted_winner,
        actual_winner=actual_winner,
        win_loss_correct=win_loss_correct,
        actual_home=actual_home,
        actual_away=actual_away,
        score_range_correct=score_range_correct,
        player_results=player_results,
        player_accuracy_pct=player_acc,
        settled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def check_calibration_signal(recent: list[PlayerSettleResult]) -> str:
    """Passive drift detection (LOCKED): fires only after 15+ settled
    results for the same player+metric. Lite: log-only flag."""
    if len(recent) < config.CALIBRATION_WINDOW:
        return "ok"
    total = len(recent)
    over = sum(1 for r in recent if r.direction == "over")
    under = sum(1 for r in recent if r.direction == "under")
    if over / total > config.DRIFT_THRESHOLD:
        return "drift_over"          # engine under-projecting this player
    if under / total > config.DRIFT_THRESHOLD:
        return "drift_under"         # engine over-projecting this player
    return "ok"
