"""
NexGame Lite — FastAPI Backend
Kage Software · 2026

The Lite dashboard API. Serves the HTML/JS frontend + JSON endpoints.
Same FastAPI foundation carries into full NexGame Phase 1 —
this is not throwaway code.

Run:
    uvicorn api.main:app --reload
    -> http://localhost:8000

Endpoints:
    GET  /                       dashboard (redirects to /login if no session)
    GET  /login                  login page
    POST /api/login               email + license_key -> session cookie
    POST /api/logout
    GET  /api/games/{sport}?date=YYYY-MM-DD   [auth required]
    GET  /api/live/{sport}       live score ticker             [auth required]
    POST /api/predict/{game_id}?sport=MLB   run 10,000 sims    [auth required]
    POST /api/read/{game_id}?regenerate=false   AI talking-points read
                                            on a locked prediction, cached
                                            after first generation
                                                                [auth required]
    GET  /api/read/{game_id}     fetch a cached read, 404 if none yet
                                                                [auth required]
    POST /api/settle/{game_id}?sport=MLB    settle vs final    [auth required]
    GET  /api/accuracy?sport=MLB  PUBLIC — the credibility engine, no login

FIRST-PITCH GUARD (added 2026-07-08):
    /api/predict now refuses any game whose provider status is not
    SCHEDULED. A "prediction" generated after first pitch is worthless
    to the public accuracy record — worse than worthless, it's the kind
    of thing a skeptic screenshots. 409 with a clear message; pass
    ?force=true to override during development/testing only. Forced
    predictions should never be settled into the public record.

LAST-OUT GUARD (added 2026-07-12):
    /api/settle now refuses to settle any game whose final boxscore
    status is not FINAL. Mirrors the first-pitch guard above — a
    settle against an in-progress/scheduled/postponed boxscore writes
    a false result into the public accuracy record (caught during
    manual testing on the NYY@WSH game, which returned a live 0-0
    boxscore and still produced a "settled" Win/Loss result client-side).
    409 with a clear message. No override flag — unlike predictions,
    there is no legitimate dev reason to force-settle against
    non-final data, so this guard has no bypass.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataclasses import asdict
from fastapi import FastAPI, HTTPException, Query, Request, Response, Depends
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from nexgame_scheduler import start_scheduler, shutdown_scheduler
import config
import auth
import customers as cust
from models import Sport, GameStatus
from ingest.base import get_provider
from engine.aggregate import run_simulation
from settle.pipeline import settle_game
from db import database as db
from whop_webhook import router as whop_router
from whop_oauth import router as whop_oauth_router

app = FastAPI(title="NexGame Lite", version="1.0")
@app.on_event("startup")
def _on_startup():
    start_scheduler()
db.init_db()
cust.init_db()
provider = get_provider()
app.include_router(whop_router)
app.include_router(whop_oauth_router)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# in-memory prediction cache for the session (also persisted to SQLite)
_predictions: dict = {}


def _sport(name: str) -> Sport:
    try:
        return Sport[name.upper()]
    except KeyError:
        raise HTTPException(400, f"Unknown sport: {name}")


def require_customer(request: Request) -> cust.Customer:
    """Dependency for every paid route. Checks the session cookie,
    re-verifies access is still active (catches mid-session
    cancellations/expirations), 401s otherwise."""
    token = request.cookies.get(auth.SESSION_COOKIE_NAME, "")
    customer = auth.get_current_customer(token)
    if not customer:
        raise HTTPException(401, "Not logged in or access has expired")
    return customer


# ── Frontend ─────────────────────────────────────────────────────────
@app.get("/")
def index(request: Request):
    token = request.cookies.get(auth.SESSION_COOKIE_NAME, "")
    if not auth.get_current_customer(token):
        # CHANGED 2026-07-18: used to redirect straight to /login,
        # skipping any chance to actually sell the product to a new
        # visitor. Now shows the public marketing/pricing page instead
        # — /login stays as its own page for RETURNING customers
        # (linked from the landing page's nav).
        return FileResponse(os.path.join(STATIC_DIR, "landing.html"))
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


# Public human-readable accuracy page. Wraps the same /api/accuracy
# endpoint the logged-in dashboard uses, but as a proper HTML view so
# marketing visitors don't get sent to raw JSON as the "proof" link.
@app.get("/accuracy")
def public_accuracy_page():
    return FileResponse(os.path.join(STATIC_DIR, "accuracy.html"))


@app.post("/api/login")
def api_login(response: Response, email: str = Query(...),
             license_key: str = Query(...)):
    result = auth.login(email, license_key)
    if not result:
        raise HTTPException(401, "Invalid email/license key, or access "
                                 "has expired")
    token, customer = result
    response.set_cookie(auth.SESSION_COOKIE_NAME, token, httponly=True,
                        max_age=auth.SESSION_TTL_SECONDS, samesite="lax")
    return {"name": customer.name, "tier": customer.tier.value}


@app.post("/api/logout")
def api_logout(response: Response):
    response.delete_cookie(auth.SESSION_COOKIE_NAME)
    return {"status": "logged_out"}


@app.get("/api/me")
def api_me(customer: cust.Customer = Depends(require_customer)):
    return {"name": customer.name, "email": customer.email,
            "tier": customer.tier.value}


# ── Games slate ───────────────────────────────────────────────────────
@app.get("/api/games/{sport_name}")
def games_for_date(sport_name: str, date: str = Query(...),
                   customer: cust.Customer = Depends(require_customer)):
    sport = _sport(sport_name)
    try:
        games = provider.get_games_for_date(sport, date)
    except Exception as e:
        raise HTTPException(502, f"Provider error: {e}")

    game_ids = [g.game_id for g in games]
    # Pull from the DB, not just the in-memory _predictions cache, so a
    # prediction/grade made earlier (or in a prior server process —
    # e.g. after a Railway redeploy) still shows up here. This is what
    # lets the Games tab display "what we called it" + "how it graded"
    # for games that already started/finished, not just ones you're
    # about to run a fresh prediction for.
    preds_by_game = db.get_predictions_for_games(game_ids)
    settles_by_game = db.get_settles_for_games(game_ids)

    out = []
    for g in games:
        pred = preds_by_game.get(g.game_id)
        settle = settles_by_game.get(g.game_id)
        pred_payload = None
        if pred:
            pred_payload = {k: v for k, v in pred.items()
                            if k not in ("id", "stored_at")}
        out.append({
            "game_id": g.game_id,
            "home": g.home_team.name, "home_abbrev": g.home_team.abbrev,
            "away": g.away_team.name, "away_abbrev": g.away_team.abbrev,
            "time": g.game_time, "venue": g.venue,
            "status": g.status.value,
            "starter_confirmed": g.home_team.confirmed_starter is not None,
            "predicted": pred is not None,
            "prediction": pred_payload,
            "settle": settle,
        })
    return out


# ── Live ticker ───────────────────────────────────────────────────────
@app.get("/api/live/{sport_name}")
def live_scores(sport_name: str,
                customer: cust.Customer = Depends(require_customer)):
    sport = _sport(sport_name)
    try:
        return provider.get_live_scores(sport)
    except Exception as e:
        raise HTTPException(502, f"Live feed error: {e}")


# ── Box score (click-through modal on the Games tab) ───────────────────
@app.get("/api/boxscore/{game_id}")
def boxscore(game_id: str, sport: str = Query(...),
            customer: cust.Customer = Depends(require_customer)):
    sp = _sport(sport)
    try:
        return provider.get_boxscore(game_id, sp)
    except Exception as e:
        raise HTTPException(502, f"Boxscore error: {e}")


# ── Prediction (the product) ─────────────────────────────────────────
@app.post("/api/predict/{game_id}")
def predict(game_id: str, sport: str = Query(...),
           runs: int = Query(default=None),
           force: bool = Query(default=False),
           customer: cust.Customer = Depends(require_customer)):
    sp = _sport(sport)
    try:
        context = provider.get_game_context(game_id, sp)
    except Exception as e:
        raise HTTPException(502, f"Could not load game: {e}")

    # FIRST-PITCH GUARD — predictions are only valid BEFORE the game
    # starts. Tonight's LAA@TEX incident: a prediction was generated
    # 3h45m after first pitch because nothing checked game status.
    # For the public accuracy record, a post-start "prediction" is
    # disqualifying, so refuse loudly instead of failing silently.
    if context.status != GameStatus.SCHEDULED and not force:
        raise HTTPException(
            409,
            f"Game is {context.status.value} — predictions must be "
            f"generated before first pitch. (Dev override: ?force=true — "
            f"forced predictions must never be settled into the public "
            f"record.)")

    pred = run_simulation(context, runs=runs)
    db.save_prediction(pred)

    payload = asdict(pred)
    payload["sport"] = pred.sport.value
    _predictions[game_id] = payload
    return payload


@app.get("/api/predict/{game_id}")
def get_prediction(game_id: str,
                   customer: cust.Customer = Depends(require_customer)):
    if game_id not in _predictions:
        raise HTTPException(404, "No prediction run for this game yet")
    return _predictions[game_id]


# ── AI Read (the script-writing helper) ──────────────────────────────
# Translates an already-locked prediction into on-camera talking
# points. Never touches the prediction itself — pure read-and-narrate
# on top of numbers that are already final.
@app.post("/api/read/{game_id}")
def generate_read(game_id: str, regenerate: bool = Query(default=False),
                  customer: cust.Customer = Depends(require_customer)):
    pred = db.get_prediction(game_id)
    if not pred:
        raise HTTPException(404, "Run a prediction before generating a read")

    if pred.get("read_text") and not regenerate:
        return {"game_id": game_id, "read": pred["read_text"], "cached": True}

    from engine.read_generator import generate_read as _generate_read
    try:
        read_text = _generate_read(pred)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"Read generation failed: {e}")

    db.save_read(game_id, read_text)
    return {"game_id": game_id, "read": read_text, "cached": False}


@app.get("/api/read/{game_id}")
def get_read(game_id: str,
            customer: cust.Customer = Depends(require_customer)):
    read_text = db.get_read(game_id)
    if read_text is None:
        raise HTTPException(404, "No prediction for this game yet")
    if not read_text:
        raise HTTPException(404, "Prediction exists but no read has "
                                 "been generated yet — POST to this "
                                 "endpoint first")
    return {"game_id": game_id, "read": read_text}


# ── Settling (the credibility engine) ────────────────────────────────
@app.post("/api/settle/{game_id}")
def settle(game_id: str, sport: str = Query(...),
          customer: cust.Customer = Depends(require_customer)):
    sp = _sport(sport)

    # BUG FIX (2026-07-16): this used to read _predictions.get(game_id) —
    # the in-memory dict, which is wiped on every server restart/redeploy.
    # A prediction made earlier the same day would 404 here as soon as
    # ANY redeploy happened in between (confirmed root cause of "today's
    # MLB game won't settle" — this app deploys multiple times a day).
    # Same DB-backed fix already applied to /api/games (see the comment
    # there) and already correctly used by the scheduler's settle_job —
    # this endpoint was just never brought in line with those two.
    stored = db.get_prediction(game_id)
    if not stored:
        raise HTTPException(404, "Run a prediction before settling")

    from nexgame_scheduler import _row_to_simulation_output
    pred = _row_to_simulation_output(stored)

    try:
        box = provider.get_final_boxscore(game_id, sp)
    except Exception as e:
        raise HTTPException(502, f"Boxscore error: {e}")

    # LAST-OUT GUARD — mirrors the first-pitch guard on /api/predict.
    # A settle against a non-final boxscore (in-progress, scheduled,
    # postponed) writes a false result into the public accuracy
    # record — disqualifying, so refuse loudly instead of failing
    # silently. No force override: unlike predictions, there's no
    # legitimate dev reason to settle against non-final data.
    box_status = box.get("status")
    if box_status != GameStatus.FINAL.value:
        raise HTTPException(
            409,
            f"Game is not final (status: {box_status}) — settling now "
            f"would write an incomplete or in-progress result into the "
            f"public accuracy record. Wait for the game to finish, "
            f"then settle again.")

    result = settle_game(pred, box)
    # home_team/away_team denormalized onto the settle row itself —
    # see save_settle / the settles table migration comment. Fixes the
    # "Recent Settled Games" list silently dropping entries.
    db.save_settle(result, home_team=pred.home_team, away_team=pred.away_team)

    out = asdict(result)
    out["player_results"] = [asdict(r) for r in result.player_results]
    return out


# ── Accuracy dashboard — PUBLIC, no login required ───────────────────
# The public track record IS the marketing. Gate it and you lose the
# whole point of the credibility engine.
@app.get("/api/accuracy")
def accuracy(sport: str = Query(default=None)):
    overall = db.get_accuracy_summary()
    by_sport = db.get_accuracy_summary(sport=sport.upper()) if sport else None
    return {
        "overall": overall,
        "by_sport": by_sport,
        # Raised from 10 to 500 (2026-07-16) — this is the public
        # "history of settled games," not a small preview. 500
        # comfortably covers realistic volume for a long while.
        "recent": db.get_recent_settles(limit=500, sport=sport),
        "provider": config.DATA_PROVIDER,
        "simulations_per_game": config.SIMULATION_RUNS,
    }
