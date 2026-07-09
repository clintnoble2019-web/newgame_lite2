"""
nexgame_scheduler.py

NOTE ON THE FILE NAME: this used to be named scheduler.py, but that
collides with a real PyPI package called "scheduler" (a totally
different job-scheduling library, unrelated to APScheduler used here).
If "scheduler" ever ends up installed in the environment -- pulled in
by another dependency, or installed by hand -- `import scheduler`
silently resolves to that package instead of this file, and every name
below (start_scheduler, shutdown_scheduler) appears to "not exist."
Renaming avoids the ambiguity entirely rather than relying on import
order.

Background jobs so predictions and settlement happen on a timer instead
of only when someone is clicking around the dashboard. This is the fix
for "Locked — LIVE" games with no prediction underneath them, and for
historical accuracy never accumulating while you're offline.

Uses the exact same provider / engine / settle imports api/main.py
already uses — no parallel logic, no reinvented sim calls.

Must be started INSIDE the deployed FastAPI process (Railway), not run
as a separate local script, or it dies the moment you close your laptop.

Add to api/main.py:
    from nexgame_scheduler import start_scheduler, shutdown_scheduler

    @app.on_event("startup")
    def _on_startup():
        start_scheduler()

    @app.on_event("shutdown")
    def _on_shutdown():
        shutdown_scheduler()
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ingest.base import get_provider
from engine.aggregate import run_simulation
from settle.pipeline import settle_game
from models import Sport, GameStatus, SimulationOutput
from db import database as db

logger = logging.getLogger("nexgame.scheduler")
logging.basicConfig(level=logging.INFO)

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
SPORTS = (Sport.MLB, Sport.NBA)

LOCK_CHECK_INTERVAL_MIN = 10     # how often to scan for games to predict
SETTLE_CHECK_INTERVAL_MIN = 15   # how often to scan for games to settle

_provider = None


def _get_provider():
    global _provider
    if _provider is None:
        _provider = get_provider()
    return _provider


def _local_date_str(offset_days: int = 0) -> str:
    return (datetime.now(LOCAL_TZ) + timedelta(days=offset_days)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Lock + predict
# ---------------------------------------------------------------------------

def lock_and_predict_job():
    """
    Scans today's and tomorrow's slate for both sports. Any game that's
    still SCHEDULED and doesn't have a prediction yet gets one. Runs
    every LOCK_CHECK_INTERVAL_MIN minutes, so games get picked up
    whenever they enter SCHEDULED with enough lead time — no need to
    guess a fixed lock window relative to first pitch/tip.
    """
    provider = _get_provider()

    for sport in SPORTS:
        for date_str in (_local_date_str(0), _local_date_str(1)):
            try:
                games = provider.get_games_for_date(sport, date_str)
            except Exception:
                logger.exception("get_games_for_date failed for %s %s", sport.value, date_str)
                continue

            for g in games:
                if g.status != GameStatus.SCHEDULED:
                    continue  # mirrors the first-pitch guard in main.py

                if db.get_prediction(g.game_id):
                    continue  # already predicted, don't overwrite on every tick

                try:
                    context = provider.get_game_context(g.game_id, sport)
                except Exception:
                    logger.exception("get_game_context failed for %s", g.game_id)
                    continue

                if context.status != GameStatus.SCHEDULED:
                    continue  # status flipped between list call and context call

                try:
                    pred = run_simulation(context, runs=None)
                    db.save_prediction(pred)
                    logger.info(
                        "Locked prediction: %s @ %s (%s)",
                        g.away_team.name, g.home_team.name, g.game_id,
                    )
                except Exception:
                    logger.exception("Prediction/save failed for %s", g.game_id)


# ---------------------------------------------------------------------------
# Settle
# ---------------------------------------------------------------------------

_PRED_FIELDS = (
    "game_id", "home_team", "away_team", "home_win_pct", "away_win_pct",
    "score_low_home", "score_med_home", "score_high_home",
    "score_low_away", "score_med_away", "score_high_away",
    "player_projections", "confidence", "win_confidence",
    "simulations_run", "generated_at",
)


def _row_to_simulation_output(row: dict) -> SimulationOutput:
    kwargs = {k: row[k] for k in _PRED_FIELDS}
    kwargs["sport"] = Sport(row["sport"])
    return SimulationOutput(**kwargs)


def settle_job():
    """
    For every prediction not yet settled, checks whether the game is
    FINAL. If so, pulls the boxscore, runs it through the same
    settle_game() pipeline main.py uses, and writes the result.
    """
    provider = _get_provider()

    for sport in SPORTS:
        unsettled = db.get_unsettled_predictions(sport=sport.value)
        if not unsettled:
            continue

        for row in unsettled:
            game_id = row["game_id"]
            try:
                context = provider.get_game_context(game_id, sport)
            except Exception:
                logger.exception("get_game_context failed while settling %s", game_id)
                continue

            if context.status != GameStatus.FINAL:
                continue  # not done yet, try again next cycle

            try:
                box = provider.get_final_boxscore(game_id, sport)
            except Exception:
                logger.exception("get_final_boxscore failed for %s", game_id)
                continue

            try:
                pred = _row_to_simulation_output(row)
                result = settle_game(pred, box)
                db.save_settle(result)
                logger.info(
                    "Settled %s -- win/loss %s",
                    game_id, "HIT" if result.win_loss_correct else "MISS",
                )
            except Exception:
                logger.exception("Settle failed for %s", game_id)


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------

_scheduler = None


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone=str(LOCAL_TZ))
    _scheduler.add_job(
        lock_and_predict_job,
        trigger=IntervalTrigger(minutes=LOCK_CHECK_INTERVAL_MIN),
        id="lock_and_predict",
        next_run_time=datetime.now(LOCAL_TZ),
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        settle_job,
        trigger=IntervalTrigger(minutes=SETTLE_CHECK_INTERVAL_MIN),
        id="settle",
        next_run_time=datetime.now(LOCAL_TZ),
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info(
        "Scheduler started: lock/predict every %sm, settle every %sm",
        LOCK_CHECK_INTERVAL_MIN, SETTLE_CHECK_INTERVAL_MIN,
    )
    return _scheduler


def shutdown_scheduler():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
