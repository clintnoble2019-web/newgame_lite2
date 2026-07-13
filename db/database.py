"""
db/database.py

Persistent SQLite layer for NexGame Lite.

Matches the actual call sites in api/main.py:
    db.init_db()
    db.save_prediction(pred)              # pred: models.SimulationOutput
    db.save_settle(result)                # result: models.GameSettleResult
    db.get_accuracy_summary(sport=None)   # sport: 'MLB' | 'NBA' | 'WNBA' | None
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

WNBA MIGRATION (2026-07-13):
    The original predictions schema had CHECK (sport IN ('MLB', 'NBA'))
    baked into the table — every WNBA save failed with IntegrityError
    the moment WNBA went live. SQLite cannot ALTER a CHECK constraint,
    so init_db now runs an idempotent rebuild migration: if the live
    table's stored schema still has the two-sport constraint, the table
    is rebuilt with the WNBA-inclusive one and ALL existing rows are
    copied over (the public MLB accuracy record survives untouched).
    Databases already migrated (or created fresh) are left alone, so
    this is safe to run on every startup.
"""

import sqlite3
import os
import json
import sys
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone

# Single source of truth for the DB path lives in config.py (which reads
# the DB_PATH env var, e.g. "/app/db/nexgame_lite.db" on Railway, inside
# the mounted Volume). Import it here instead of computing a separate
# path, so the scheduler and api/main.py always write to the same file.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

DB_PATH = config.DB_PATH

# One place to grow the allowed-sport list from now on. The CREATE
# TABLE below and the migration both derive from this.
ALLOWED_SPORTS = ("MLB", "NBA", "WNBA", "CS2")
_SPORT_CHECK = "CHECK (sport IN ('MLB', 'NBA', 'WNBA', 'CS2'))"


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
            f"""
            CREATE TABLE IF NOT EXISTS predictions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id             TEXT UNIQUE NOT NULL,
                sport               TEXT NOT NULL {_SPORT_CHECK},
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

        # Migration: pitching_matchup added 2026-07-11. CREATE TABLE IF
        # NOT EXISTS won't touch an existing table, so add the column
        # defensively for DBs created before this change. New DBs get it
        # via the ALTER too (kept out of the CREATE above so both paths
        # go through the same statement — one source of truth).
        try:
            conn.execute("ALTER TABLE predictions ADD COLUMN "
                         "pitching_matchup TEXT NOT NULL DEFAULT '{}'")
        except sqlite3.OperationalError:
            pass   # column already exists

    # WNBA migration runs on its own connection (needs foreign_keys OFF
    # for the table rebuild — get_conn() forces it ON).
    _migrate_sport_check()


def _migrate_sport_check() -> None:
    """Rebuild the predictions table if its live schema still carries
    the old two-sport CHECK constraint. Idempotent: no-ops on already-
    migrated or freshly created databases.

    SQLite cannot ALTER a CHECK constraint, so this follows the
    documented rebuild recipe: new table -> copy every row -> drop old
    -> rename -> recreate indexes, inside one transaction with
    foreign_keys OFF (settles references predictions(game_id); the
    rename preserves the reference target name so the FK survives)."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='predictions'"
        ).fetchone()
        if not row or not row[0]:
            return
        schema_sql = row[0]
        if "'CS2'" in schema_sql:
            return   # already fully migrated (includes the latest sport)
        if "CHECK" not in schema_sql.upper():
            return   # no constraint to migrate

        conn.execute("PRAGMA foreign_keys=OFF;")
        conn.execute("BEGIN;")

        # Old table may or may not have pitching_matchup depending on
        # when it was created — copy exactly the columns both share.
        old_cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(predictions);").fetchall()]

        conn.execute(
            f"""
            CREATE TABLE predictions_new (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id             TEXT UNIQUE NOT NULL,
                sport               TEXT NOT NULL {_SPORT_CHECK},
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
                player_projections  TEXT NOT NULL,
                confidence          TEXT NOT NULL,
                win_confidence      TEXT NOT NULL,
                simulations_run     INTEGER NOT NULL,
                generated_at        TEXT NOT NULL,
                stored_at           TEXT NOT NULL,
                pitching_matchup    TEXT NOT NULL DEFAULT '{{}}'
            );
            """
        )

        new_cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(predictions_new);").fetchall()]
        shared = [c for c in old_cols if c in new_cols]
        col_list = ", ".join(shared)
        conn.execute(
            f"INSERT INTO predictions_new ({col_list}) "
            f"SELECT {col_list} FROM predictions;"
        )

        conn.execute("DROP TABLE predictions;")
        conn.execute("ALTER TABLE predictions_new RENAME TO predictions;")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_sport "
                     "ON predictions(sport);")
        conn.execute("COMMIT;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()


def _sport_value(sport) -> str:
    """Handles both the Sport enum and a plain string ('MLB'/'NBA'/'WNBA')."""
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
                simulations_run, generated_at, stored_at, pitching_matchup
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                stored_at = excluded.stored_at,
                pitching_matchup = excluded.pitching_matchup
            """,
            (
                pred.game_id, _sport_value(pred.sport), pred.home_team, pred.away_team,
                pred.home_win_pct, pred.away_win_pct,
                pred.score_low_home, pred.score_med_home, pred.score_high_home,
                pred.score_low_away, pred.score_med_away, pred.score_high_away,
                json.dumps(pred.player_projections), pred.confidence, pred.win_confidence,
                pred.simulations_run, pred.generated_at,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                json.dumps(getattr(pred, "pitching_matchup", {}) or {}),
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
    d["pitching_matchup"] = json.loads(d.get("pitching_matchup") or "{}")
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
        d["pitching_matchup"] = json.loads(d.get("pitching_matchup") or "{}")
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

    Key names below (win_loss_pct / score_range_pct / player_total_pct) are
    the frontend contract — api/static/index.html reads these exact keys
    off the /api/accuracy response, as 0-100 percentages already rounded.
    (Previously this returned win_loss_accuracy/score_range_accuracy/
    avg_player_accuracy_pct as 0-1 fractions under different names, which
    the frontend didn't recognize -> rendered as "undefined%".)
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
            "win_loss_pct": 0.0,
            "score_range_pct": 0.0,
            "player_total_pct": 0.0,
        }

    win_loss_hits = sum(r["win_loss_correct"] for r in rows)
    score_range_hits = sum(r["score_range_correct"] for r in rows)
    avg_player_acc = sum(r["player_accuracy_pct"] for r in rows) / total

    return {
        "sport": sport or "ALL",
        "total_settled": total,
        "win_loss_pct": round(win_loss_hits / total * 100, 1),
        "score_range_pct": round(score_range_hits / total * 100, 1),
        "player_total_pct": round(avg_player_acc, 1),
    }


def get_recent_settles(limit: int = 10) -> list[dict]:
    """Matches main.py: db.get_recent_settles(limit=10).

    JOINs predictions for home_team/away_team — the settles table only
    stores actual_home/actual_away scores, not team names, so without
    the join the frontend's `${s.home_team} @ ${s.away_team}` read
    undefined off every row ("undefined @ undefined" on the Accuracy tab).
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.*, p.home_team AS home_team, p.away_team AS away_team
            FROM settles s
            JOIN predictions p ON p.game_id = s.game_id
            ORDER BY s.settled_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["player_results"] = json.loads(d["player_results"])
        d["win_loss_correct"] = bool(d["win_loss_correct"])
        d["score_range_correct"] = bool(d["score_range_correct"])
        out.append(d)
    return out


def get_predictions_for_games(game_ids: list[str]) -> dict:
    """Bulk-fetch stored predictions for a list of game_ids, keyed by
    game_id. Powers the Games tab showing a prediction that was run
    earlier — even in a previous server process, since the old
    in-memory-only _predictions cache in main.py didn't survive a
    restart/redeploy."""
    if not game_ids:
        return {}
    placeholders = ",".join("?" for _ in game_ids)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM predictions WHERE game_id IN ({placeholders})",
            game_ids,
        ).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        d["player_projections"] = json.loads(d["player_projections"])
        d["pitching_matchup"] = json.loads(d.get("pitching_matchup") or "{}")
        out[d["game_id"]] = d
    return out


def get_settles_for_games(game_ids: list[str]) -> dict:
    """Bulk-fetch settle results for a list of game_ids, keyed by
    game_id. Powers the per-game grade shown on the Games tab."""
    if not game_ids:
        return {}
    placeholders = ",".join("?" for _ in game_ids)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM settles WHERE game_id IN ({placeholders})",
            game_ids,
        ).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        d["player_results"] = json.loads(d["player_results"])
        d["win_loss_correct"] = bool(d["win_loss_correct"])
        d["score_range_correct"] = bool(d["score_range_correct"])
        out[d["game_id"]] = d
    return out
