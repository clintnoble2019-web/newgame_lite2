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

MISSED-GAMES GAP (2026-07-17):
    lock_and_predict_job only ever creates a prediction while a game is
    still SCHEDULED, checked every LOCK_CHECK_INTERVAL_MIN. settle_job
    only ever settles a game that already HAS a prediction row. Nothing
    previously covered the gap between those two: a game that goes
    straight from "not yet scanned" to FINAL/LIVE without ever being
    caught in SCHEDULED status — a doubleheader nightcap added same-day,
    a game rescheduled after that day's scan cycle already ran, or a
    brief window where the process restarted and missed a tick.

    That game gets no prediction, ever, from either job — which means
    settle_job has nothing to check it against, and it silently never
    appears in the settled history or the accuracy numbers. No error,
    no log line, nothing on the dashboard. Confirmed live: CHW @ TOR
    (FINAL 12-4) and a TB @ BOS doubleheader nightcap both fell through
    this exact gap on 2026-07-17.

    catch_missed_games_job() closes it: on the same timer as settle_job,
    it scans today's and yesterday's slate for games that are FINAL or
    LIVE, have no existing prediction row, and aren't POSTPONED/TBD
    (nothing happened for those, so nothing should be predicted). Any
    match gets a prediction generated the normal way — run_simulation()
    only ever looks at pre-game team/roster/pitching data, never the
    actual score, so this isn't "predicting" a result it already knows
    — and the prediction is saved with late_locked=True so it stays
    distinguishable from a normal, on-time lock. Once saved, settle_job
    picks it up on its own next tick like any other unsettled
    prediction — no separate settle logic needed here.

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

KALSHI ODDS BACKFILL (2026-07-14):
    lock_and_predict_job deliberately never re-predicts a game it's
    already locked (see the comment on that skip below) — but that
    means a game predicted BEFORE a Kalshi market opened stayed
    permanently frozen with no odds, since nothing ever re-checked it.
    settle_job already calls get_game_context on every unsettled
    prediction each tick (to check FINAL status) — and get_game_context
    already re-attaches fresh Kalshi odds every time it runs (see
    ingest/balldontlie_provider.py's _attach_kalshi_odds_to_shell).
    That data was just being fetched and discarded. Now it's written
    back via db.update_kalshi_odds() — a targeted update that touches
    ONLY the three Kalshi columns, never the locked prediction fields
    that feed the graded win%/score-range/player projections.
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
SPORTS = (Sport.MLB, Sport.NBA, Sport.WNBA, Sport.CS2)

LOCK_CHECK_INTERVAL_MIN = 10     # how often to scan for games to predict
SETTLE_CHECK_INTERVAL_MIN = 15   # how often to scan for games to settle
CATCH_MISSED_INTERVAL_MIN = 15   # how often to scan for games that slipped
                                  # past lock_and_predict_job entirely

# Statuses catch_missed_games_job will backfill a late prediction for.
# Deliberately excludes SCHEDULED (that's lock_and_predict_job's job,
# not this one's) and POSTPONED/TBD (nothing happened, so there's
# nothing to blindly predict against).
_CATCHABLE_STATUSES = (GameStatus.LIVE, GameStatus.FINAL)

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
# Catch missed games — games that went FINAL/LIVE without ever passing
# through lock_and_predict_job while SCHEDULED (see module docstring).
# ---------------------------------------------------------------------------

def catch_missed_games_job():
    """
    Scans today's and yesterday's slate for both sports. Any game that's
    FINAL or LIVE, has no prediction row yet, and isn't POSTPONED/TBD
    gets a late prediction generated and saved with late_locked=True.

    Yesterday is included (not just today) because the failure mode
    this fixes is specifically games that were NEVER scanned while
    SCHEDULED — a late-night final can still be sitting there
    unpredicted the next morning otherwise.

    Deliberately does NOT settle anything itself — it only creates the
    missing prediction row. settle_job already re-scans every
    unsettled prediction every SETTLE_CHECK_INTERVAL_MIN and will pick
    this up on its own next tick, so there's no reason to duplicate
    that logic here.
    """
    provider = _get_provider()

    for sport in SPORTS:
        for date_str in (_local_date_str(0), _local_date_str(-1)):
            try:
                games = provider.get_games_for_date(sport, date_str)
            except Exception:
                logger.exception(
                    "catch_missed_games_job: get_games_for_date failed "
                    "for %s %s", sport.value, date_str,
                )
                continue

            for g in games:
                if g.status not in _CATCHABLE_STATUSES:
                    continue  # SCHEDULED is lock_and_predict_job's job;
                              # POSTPONED/TBD have nothing to predict

                if db.get_prediction(g.game_id):
                    continue  # already predicted (normally or late) —
                              # never overwrite an existing lock

                try:
                    context = provider.get_game_context(g.game_id, sport)
                except Exception:
                    logger.exception(
                        "catch_missed_games_job: get_game_context "
                        "failed for %s", g.game_id,
                    )
                    continue

                try:
                    pred = run_simulation(context, runs=None)
                    pred.late_locked = True
                    db.save_prediction(pred)
                    logger.warning(
                        "LATE-LOCKED prediction (missed while SCHEDULED): "
                        "%s @ %s (%s, status=%s) — check why "
                        "lock_and_predict_job never caught this one",
                        g.away_team.name, g.home_team.name,
                        g.game_id, g.status.value,
                    )
                except Exception:
                    logger.exception(
                        "catch_missed_games_job: prediction/save failed "
                        "for %s", g.game_id,
                    )


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

    KALSHI BACKFILL: also runs on EVERY unsettled prediction, whether
    or not the game is final yet — get_game_context already re-runs
    the Kalshi match as a side effect (see module docstring), so this
    is a zero-extra-cost way to catch odds that appeared after the
    original prediction was locked. Only writes when something
    actually changed, to avoid pointless DB churn every 15 minutes.
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

            # KALSHI BACKFILL — safe on any tick, settled or not: only
            # touches the three odds columns, never the locked
            # prediction fields that feed grading.
            new_ticker = getattr(context, "kalshi_event_ticker", "") or ""
            stored_ticker = row.get("kalshi_event_ticker") or ""
            if new_ticker and new_ticker != stored_ticker:
                try:
                    db.update_kalshi_odds(
                        game_id, new_ticker,
                        getattr(context, "kalshi_home_prob", 0.0) or 0.0,
                        getattr(context, "kalshi_away_prob", 0.0) or 0.0,
                    )
                    logger.info(
                        "Backfilled Kalshi odds for %s (%s)",
                        game_id, new_ticker,
                    )
                except Exception:
                    logger.exception(
                        "Kalshi odds backfill failed for %s", game_id)

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
                db.save_settle(result, home_team=pred.home_team, away_team=pred.away_team)
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
        catch_missed_games_job,
        trigger=IntervalTrigger(minutes=CATCH_MISSED_INTERVAL_MIN),
        id="catch_missed_games",
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
        "Scheduler started: lock/predict every %sm, catch-missed every "
        "%sm, settle every %sm",
        LOCK_CHECK_INTERVAL_MIN, CATCH_MISSED_INTERVAL_MIN,
        SETTLE_CHECK_INTERVAL_MIN,
    )
    return _scheduler


def shutdown_scheduler():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
