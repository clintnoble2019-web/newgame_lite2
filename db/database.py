"""
db/database.py

Persistent SQLite layer for NexGame Lite.

Matches the actual call sites in api/main.py:
    db.init_db()
    db.save_prediction(pred)              # pred: models.SimulationOutput
    db.save_settle(result)                # result: models.GameSettleResult
    db.get_accuracy_summary(sport=None)   # sport: 'MLB' | 'NBA' | None
    db.get_recent_settles(limit=10)

Plus two additions the scheduler needs (nothing else in main.py touches
these, so they're additive, not a behavior change):
    db.get_prediction(game_id)            -> dict | None, for settling
    db.get_unsettled_predictions(sport=None) -> games predicted but not
                                                 yet settled

SimulationOutput.sport and GameContext-derived fields use the Sport enum;
GameSettleResult.sport is already a plain string (see models.py). Both
are normalized to the enum's .value string on write.

player_projections (dict) and player_results (list of PlayerSettleResult)
are stored as JSON text columns — SQLite has no native nested type.

Drop in at: nexgame_lite/db/database.py (nexgame_lite/db/__init__.py
must exist — empty file is fine, main.py already imports `from db import
database as db` so the package is presumably already set up).
"""

import sqlite3
import os
import json
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "nexgame.db")


@contextmanager
def get_conn():
    """
    WAL mode so the scheduler's background thread and FastAPI request
    handlers can both touch the DB without blocking each other.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS predictions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id             TEXT UNIQUE NOT NULL,
                sport               TEXT NOT NULL CHECK (sport IN ('MLB', 'NBA')),
                home_team           TEXT NOT NULL,
                away_team           TEXT NOT NULL,
                home_win_pct        REAL NOT NULL,
                away_win_pct        REAL NOT NULL,
                score_low_home      INTEGER NOT NULL,
                score_med_home      INTEGER NOT NULL,
                score_high_home     INTEGER NOT NULL,
                score_low_away      INTEGER NOT NULL,
                score_med_away      INTEGER NOT NULL,
                score_high_away     INTEGER NOT NULL,
                player_projections  TEXT NOT NULL,   -- JSON: dict
                confidence          TEXT NOT NULL,
                win_confidence      TEXT NOT NULL,
                simulations_run     INTEGER NOT NULL,
                generated_at        TEXT NOT NULL,
                stored_at           TEXT NOT NULL
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settles (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id               TEXT UNIQUE NOT NULL
                                      REFERENCES predictions(game_id) ON DELETE CASCADE,
                sport                 TEXT NOT NULL,
                predicted_winner      TEXT NOT NULL,   -- 'home' | 'away'
                actual_winner         TEXT NOT NULL,   -- 'home' | 'away'
                win_loss_correct      INTEGER NOT NULL,
                actual_home           INTEGER NOT NULL,
                actual_away           INTEGER NOT NULL,
                score_range_correct   INTEGER NOT NULL,
                player_results        TEXT NOT NULL,   -- JSON: list[PlayerSettleResult]
                player_accuracy_pct   REAL NOT NULL,
                settled_at            TEXT NOT NULL
            );
            """
        )

        conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_sport ON predictions(sport);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_settles_sport ON settles(sport);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_settles_settled_at ON settles(settled_at);")


def _sport_value(sport) -> str:
    """Handles both the Sport enum and a plain string ('MLB'/'NBA')."""
    return sport.value if hasattr(sport, "value") else str(sport)


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------

def save_prediction(pred) -> None:
    """
    pred: models.SimulationOutput

    Upsert on game_id — if /api/predict is re-run for the same game
    (e.g. an SP confirmation update before first pitch), this overwrites
    the prior prediction rather than erroring.
    """
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO predictions (
                game_id, sport, home_team, away_team,
                home_win_pct, away_win_pct,
                score_low_home, score_med_home, score_high_home,
                score_low_away, score_med_away, score_high_away,
                player_projections, confidence, win_confidence,
                simulations_run, generated_at, stored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                home_win_pct = excluded.home_win_pct,
                away_win_pct = excluded.away_win_pct,
                score_low_home = excluded.score_low_home,
                score_med_home = excluded.score_med_home,
                score_high_home = excluded.score_high_home,
                score_low_away = excluded.score_low_away,
                score_med_away = excluded.score_med_away,
                score_high_away = excluded.score_high_away,
                player_projections = excluded.player_projections,
                confidence = excluded.confidence,
                win_confidence = excluded.win_confidence,
                simulations_run = excluded.simulations_run,
                generated_at = excluded.generated_at,
                stored_at = excluded.stored_at
            """,
            (
                pred.game_id, _sport_value(pred.sport), pred.home_team, pred.away_team,
                pred.home_win_pct, pred.away_win_pct,
                pred.score_low_home, pred.score_med_home, pred.score_high_home,
                pred.score_low_away, pred.score_med_away, pred.score_high_away,
                json.dumps(pred.player_projections), pred.confidence, pred.win_confidence,
                pred.simulations_run, pred.generated_at,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )


def get_prediction(game_id: str) -> dict | None:
    """
    Returns the raw row as a dict with player_projections parsed back
    from JSON. Used by the scheduler to rebuild a SimulationOutput for
    settle_game() after a restart (the in-memory _predictions cache in
    main.py won't survive that, but this will).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM predictions WHERE game_id = ?", (game_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["player_projections"] = json.loads(d["player_projections"])
    return d


def get_unsettled_predictions(sport: str = None) -> list[dict]:
    """
    Predictions with no matching row in `settles` yet — the scheduler's
    settle_job() candidate list. Doesn't know game status on its own;
    caller still needs to check the provider for FINAL before settling.
    """
    query = """
        SELECT p.* FROM predictions p
        LEFT JOIN settles s ON s.game_id = p.game_id
        WHERE s.game_id IS NULL
    """
    params = []
    if sport:
        query += " AND p.sport = ?"
        params.append(sport.upper())

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["player_projections"] = json.loads(d["player_projections"])
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Settling
# ---------------------------------------------------------------------------

def save_settle(result) -> None:
    """
    result: models.GameSettleResult

    result.sport is already a plain string (see models.py — unlike
    SimulationOutput.sport, which is the enum). result.player_results is
    a list[PlayerSettleResult] dataclasses; serialized to JSON.
    """
    player_results_json = json.dumps([
        asdict(r) if is_dataclass(r) else r for r in result.player_results
    ])

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settles (
                game_id, sport, predicted_winner, actual_winner,
                win_loss_correct, actual_home, actual_away,
                score_range_correct, player_results, player_accuracy_pct,
                settled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                predicted_winner = excluded.predicted_winner,
                actual_winner = excluded.actual_winner,
                win_loss_correct = excluded.win_loss_correct,
                actual_home = excluded.actual_home,
                actual_away = excluded.actual_away,
                score_range_correct = excluded.score_range_correct,
                player_results = excluded.player_results,
                player_accuracy_pct = excluded.player_accuracy_pct,
                settled_at = excluded.settled_at
            """,
            (
                result.game_id, result.sport, result.predicted_winner, result.actual_winner,
                int(result.win_loss_correct), result.actual_home, result.actual_away,
                int(result.score_range_correct), player_results_json, result.player_accuracy_pct,
                result.settled_at,
            ),
        )


# ---------------------------------------------------------------------------
# Accuracy — public endpoint, /api/accuracy
# ---------------------------------------------------------------------------

def get_accuracy_summary(sport: str = None) -> dict:
    """
    Matches main.py:
        db.get_accuracy_summary()                -> overall
        db.get_accuracy_summary(sport='MLB')      -> by_sport
    """
    query = "SELECT win_loss_correct, score_range_correct, player_accuracy_pct FROM settles"
    params = []
    if sport:
        query += " WHERE sport = ?"
        params.append(sport.upper())

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    total = len(rows)
    if total == 0:
        return {
            "sport": sport or "ALL",
            "total_settled": 0,
            "win_loss_accuracy": None,
            "score_range_accuracy": None,
            "avg_player_accuracy_pct": None,
        }

    win_loss_hits = sum(r["win_loss_correct"] for r in rows)
    score_range_hits = sum(r["score_range_correct"] for r in rows)
    avg_player_acc = sum(r["player_accuracy_pct"] for r in rows) / total

    return {
        "sport": sport or "ALL",
        "total_settled": total,
        "win_loss_accuracy": round(win_loss_hits / total, 4),
        "score_range_accuracy": round(score_range_hits / total, 4),
        "avg_player_accuracy_pct": round(avg_player_acc, 1),
    }


def get_recent_settles(limit: int = 10) -> list[dict]:
    """Matches main.py: db.get_recent_settles(limit=10)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM settles ORDER BY settled_at DESC LIMIT ?", (limit,)
        ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["player_results"] = json.loads(d["player_results"])
        d["win_loss_correct"] = bool(d["win_loss_correct"])
        d["score_range_correct"] = bool(d["score_range_correct"])
        out.append(d)
    return out
