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
    POST /api/settle/{game_id}?sport=MLB    settle vs final    [auth required]
    GET  /api/accuracy?sport=MLB  PUBLIC — the credibility engine, no login

FIRST-PITCH GUARD (added 2026-07-08):
    /api/predict now refuses any game whose provider status is not
    SCHEDULED. A "prediction" generated after first pitch is worthless
    to the public accuracy record — worse than worthless, it's the kind
    of thing a skeptic screenshots. 409 with a clear message; pass
    ?force=true to override during development/testing only. Forced
    predictions should never be settled into the public record.
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
from gumroad_webhook import router as gumroad_router

app = FastAPI(title="NexGame Lite", version="1.0")
@app.on_event("startup")
def _on_startup():
    start_scheduler()
db.init_db()
cust.init_db()
provider = get_provider()
app.include_router(gumroad_router)

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
        return RedirectResponse("/login")
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


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
    return [{
        "game_id": g.game_id,
        "home": g.home_team.name, "home_abbrev": g.home_team.abbrev,
        "away": g.away_team.name, "away_abbrev": g.away_team.abbrev,
        "time": g.game_time, "venue": g.venue,
        "status": g.status.value,
        "starter_confirmed": g.home_team.confirmed_starter is not None,
        "predicted": g.game_id in _predictions,
    } for g in games]


# ── Live ticker ───────────────────────────────────────────────────────
@app.get("/api/live/{sport_name}")
def live_scores(sport_name: str,
                customer: cust.Customer = Depends(require_customer)):
    sport = _sport(sport_name)
    try:
        return provider.get_live_scores(sport)
    except Exception as e:
        raise HTTPException(502, f"Live feed error: {e}")


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


# ── Settling (the credibility engine) ────────────────────────────────
@app.post("/api/settle/{game_id}")
def settle(game_id: str, sport: str = Query(...),
          customer: cust.Customer = Depends(require_customer)):
    sp = _sport(sport)
    payload = _predictions.get(game_id)
    if not payload:
        raise HTTPException(404, "Run a prediction before settling")

    # rebuild SimulationOutput from cached payload
    from models import SimulationOutput
    pred = SimulationOutput(**{**payload, "sport": sp})

    try:
        box = provider.get_final_boxscore(game_id, sp)
    except Exception as e:
        raise HTTPException(502, f"Boxscore error: {e}")

    result = settle_game(pred, box)
    db.save_settle(result)

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
        "recent": db.get_recent_settles(limit=10),
        "provider": config.DATA_PROVIDER,
        "simulations_per_game": config.SIMULATION_RUNS,
    }
