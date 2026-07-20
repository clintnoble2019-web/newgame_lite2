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

KALSHI COLUMNS (2026-07-14):
    Three new columns on predictions — kalshi_event_ticker (TEXT),
    kalshi_home_prob (REAL), kalshi_away_prob (REAL) — added the same
    safe way pitching_matchup was: a plain ALTER TABLE ADD COLUMN
    wrapped in try/except OperationalError. Unlike the CHECK-constraint
    migration, this doesn't need a table rebuild (adding a nullable-
    with-default column is always safe in SQLite). Old rows get the
    defaults ('' / 0.0); new predictions carry real values when a
    Kalshi market was matched.

LATE_LOCKED COLUMN (2026-07-17):
    One new column on predictions — late_locked (INTEGER, 0/1 as a
    bool) — same safe additive ALTER pattern. Set to 1 only when
    nexgame_scheduler.catch_missed_games_job() had to generate a
    prediction for a game that was already FINAL/LIVE because
    lock_and_predict_job never caught it while SCHEDULED (see that
    job's docstring for why games slip through). Every prediction
    made the normal way stays 0/False. This is what fixes games
    silently never appearing in the settled history at all — see
    nexgame_scheduler.py for the actual root-cause writeup.
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

# Fix 2026-07-19: sqlite3.connect() can create the .db FILE itself if
# missing, but NOT a missing parent directory — confirmed live crash:
# "sqlite3.OperationalError: unable to open database file" the moment
# DB_PATH was pointed at a freshly-attached Railway Volume mount path
# (/app/data/nexgame_lite.db). The volume mounts fine; this just makes
# sure the directory component of DB_PATH actually exists before any
# connection is attempted, regardless of whether that's a fresh volume
# or a plain relative filename (os.path.dirname("") is "", and
# makedirs on an empty string is a safe no-op).
if os.path.dirname(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

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
                stored_at           TEXT NOT NULL,
                late_locked         INTEGER NOT NULL DEFAULT 0
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

        # Migration: Kalshi columns added 2026-07-14. Same safe
        # additive pattern — no table rebuild needed, just three new
        # nullable-with-default columns.
        for ddl in (
            "ALTER TABLE predictions ADD COLUMN "
            "kalshi_event_ticker TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE predictions ADD COLUMN "
            "kalshi_home_prob REAL NOT NULL DEFAULT 0.0",
            "ALTER TABLE predictions ADD COLUMN "
            "kalshi_away_prob REAL NOT NULL DEFAULT 0.0",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass   # column already exists

        # Migration: read_text added 2026-07-16 — caches the AI-generated
        # natural-language "read" of a locked prediction (the on-camera
        # talking-point version). Empty string = not generated yet.
        # Nullable-with-default, same safe additive pattern as above.
        try:
            conn.execute("ALTER TABLE predictions ADD COLUMN "
                         "read_text TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass   # column already exists

        # Migration: late_locked added 2026-07-17. Same safe additive
        # pattern as pitching_matchup/read_text — nullable-with-default,
        # no rebuild needed. Existing rows default to 0 (they were all
        # locked the normal, pre-game way — this column didn't exist
        # yet, so there's nothing to backfill).
        try:
            conn.execute("ALTER TABLE predictions ADD COLUMN "
                         "late_locked INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass   # column already exists

        # Migration: settles.home_team / settles.away_team added
        # 2026-07-16. BUG FOUND: get_recent_settles() previously required
        # an INNER JOIN to predictions to get team names, which meant a
        # settled game could silently vanish from the public "settled
        # games" history if its predictions row was ever missing —
        # fragile for a feature whose whole point is being a trustworthy
        # public record. Denormalizing the team names directly onto the
        # settle row at settle-time removes that dependency entirely.
        for ddl in (
            "ALTER TABLE settles ADD COLUMN home_team TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE settles ADD COLUMN away_team TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(ddl)
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
                pitching_matchup    TEXT NOT NULL DEFAULT '{{}}',
                late_locked         INTEGER NOT NULL DEFAULT 0
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

def update_kalshi_odds(game_id: str, event_ticker: str,
                       home_prob: float, away_prob: float) -> None:
    """Refresh ONLY the three Kalshi columns on an already-stored
    prediction — added 2026-07-14 to fix a real gap: a game predicted
    before a Kalshi market opened stayed permanently frozen with no
    odds, since lock_and_predict_job never re-predicts a game it's
    already locked (by design — re-running the simulation on a tick
    would silently drift win%/score ranges after the fact, undermining
    the whole point of a locked, gradeable prediction).

    Kalshi odds are pure display data — they never feed the model's
    win%, score ranges, or anything that gets graded — so refreshing
    them on a tick is safe in a way re-running the simulation isn't.
    This function ONLY updates these three columns; every other field
    on the prediction stays exactly as originally locked."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE predictions SET
                kalshi_event_ticker = ?,
                kalshi_home_prob = ?,
                kalshi_away_prob = ?
            WHERE game_id = ?
            """,
            (event_ticker or "", home_prob or 0.0, away_prob or 0.0, game_id),
        )


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
                simulations_run, generated_at, stored_at, pitching_matchup,
                kalshi_event_ticker, kalshi_home_prob, kalshi_away_prob,
                late_locked
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                pitching_matchup = excluded.pitching_matchup,
                kalshi_event_ticker = excluded.kalshi_event_ticker,
                kalshi_home_prob = excluded.kalshi_home_prob,
                kalshi_away_prob = excluded.kalshi_away_prob,
                late_locked = excluded.late_locked
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
                getattr(pred, "kalshi_event_ticker", "") or "",
                getattr(pred, "kalshi_home_prob", 0.0) or 0.0,
                getattr(pred, "kalshi_away_prob", 0.0) or 0.0,
                int(bool(getattr(pred, "late_locked", False))),
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


def save_read(game_id: str, read_text: str) -> None:
    """Cache the AI-generated read for a game. Only ever updates the
    read_text column — never touches the locked prediction fields, so
    generating (or regenerating) a read can't accidentally drift the
    graded prediction it's describing."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE predictions SET read_text = ? WHERE game_id = ?",
            (read_text, game_id),
        )


def get_read(game_id: str) -> str | None:
    """Returns the cached read text, '' if a prediction exists but no
    read's been generated yet, or None if there's no prediction at all
    for this game_id (caller should treat that as 'run a prediction
    first', not 'read failed')."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT read_text FROM predictions WHERE game_id = ?",
            (game_id,),
        ).fetchone()
    if row is None:
        return None
    return row["read_text"]


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

def save_settle(result, home_team: str = "", away_team: str = "") -> None:
    """
    result: models.GameSettleResult

    result.sport is already a plain string (see models.py — unlike
    SimulationOutput.sport, which is the enum). result.player_results is
    a list[PlayerSettleResult] dataclasses; serialized to JSON.

    home_team/away_team: denormalized onto the settle row itself
    (2026-07-16) so the public settled-games history never depends on
    the predictions row still existing — see the migration comment in
    init_db. Callers should pass these from the SimulationOutput they
    already have in hand (pred.home_team / pred.away_team) rather than
    relying on a join at read time.
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
                settled_at, home_team, away_team
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                predicted_winner = excluded.predicted_winner,
                actual_winner = excluded.actual_winner,
                win_loss_correct = excluded.win_loss_correct,
                actual_home = excluded.actual_home,
                actual_away = excluded.actual_away,
                score_range_correct = excluded.score_range_correct,
                player_results = excluded.player_results,
                player_accuracy_pct = excluded.player_accuracy_pct,
                settled_at = excluded.settled_at,
                home_team = excluded.home_team,
                away_team = excluded.away_team
            """,
            (
                result.game_id, result.sport, result.predicted_winner, result.actual_winner,
                int(result.win_loss_correct), result.actual_home, result.actual_away,
                int(result.score_range_correct), player_results_json, result.player_accuracy_pct,
                result.settled_at, home_team, away_team,
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

    CS2 EXCLUDED FROM THE BLENDED "ALL" NUMBER (2026-07-14): CS2 roster
    composition varies too much match-to-match (frequent stand-ins) to
    track consistently against a fixed prediction the way MLB/NBA/WNBA
    rosters do — folding CS2 into the headline accuracy % would blend in
    a fundamentally different reliability signal. CS2 win/loss still
    settles and displays normally on individual game cards; it's only
    excluded from THIS blended figure. An explicit sport='CS2' query
    still returns real computed numbers (unchanged) — the exclusion is
    only applied when sport is None (the "overall" blend).
    """
    query = "SELECT win_loss_correct, score_range_correct, player_accuracy_pct FROM settles"
    params = []
    if sport:
        query += " WHERE sport = ?"
        params.append(sport.upper())
    else:
        query += " WHERE sport != 'CS2'"

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


def get_recent_settles(limit: int = 500, sport: str = None) -> list[dict]:
    """Matches main.py: db.get_recent_settles(limit=500).

    BUG FIX (2026-07-16): previously used an INNER JOIN to predictions
    for home_team/away_team, since settles didn't store them directly.
    That meant ANY settled game whose predictions row was missing —
    for any reason — silently vanished from this list entirely, even
    though get_accuracy_summary() (which queries settles alone, no
    join) still counted it in the headline percentages. That mismatch
    is exactly what caused the public dashboard to show real win/loss
    stats while "Recent Settled Games" showed none.

    Fixed by denormalizing home_team/away_team onto the settles row at
    settle-time (see save_settle). This now LEFT JOINs only as a
    fallback for old rows settled before the denormalization existed —
    COALESCE prefers the settle row's own team names first, so a
    missing predictions row can never make a settled game disappear
    from its own history again.

    Default limit raised from 10 to 500 — this feed is the public
    "history of settled games," not a small preview, and 500 comfortably
    covers realistic volume for a long while without needing pagination.

    CS2 EXCLUSION FIX (2026-07-19): previously excluded CS2 with an
    unconditional `WHERE s.sport != 'CS2'`, applied even when the
    caller explicitly asked for sport='CS2'. That meant the accuracy
    dashboard's league dropdown could show real CS2 win/loss numbers
    (from get_accuracy_summary, which already excluded CS2 correctly
    — only from the blended ALL view) sitting directly above an empty
    "no settled games" list, since this function silently dropped
    every CS2 row regardless of the filter. Now CS2 is only excluded
    when no specific sport was requested (the "All Sports" blend),
    exactly matching get_accuracy_summary's existing, correct pattern.
    """
    query = """
        SELECT s.id, s.game_id, s.sport, s.predicted_winner, s.actual_winner,
               s.win_loss_correct, s.actual_home, s.actual_away,
               s.score_range_correct, s.player_results, s.player_accuracy_pct,
               s.settled_at,
               COALESCE(NULLIF(s.home_team, ''), p.home_team) AS home_team,
               COALESCE(NULLIF(s.away_team, ''), p.away_team) AS away_team
        FROM settles s
        LEFT JOIN predictions p ON p.game_id = s.game_id
    """
    params = []
    if sport:
        query += " WHERE s.sport = ?"
        params.append(sport.upper())
    else:
        query += " WHERE s.sport != 'CS2'"
    query += " ORDER BY s.settled_at DESC LIMIT ?"
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

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
    game_id. Powers the per-game grade shown on the Games tab.

    JOINS predictions for home_team/away_team (2026-07-13 fix — same
    reasoning as get_recent_settles' join). WITHOUT this, the frontend
    fell back to re-fetching the live games list to label 'home'/
    'away' as real team names — and a live re-fetch isn't guaranteed
    to return teams in the same order as when the pick was originally
    made (observed live on CS2: BallDontLie's match team1/team2 order
    isn't provably stable across separate calls for the same match).
    That caused a genuinely correct, correctly-graded pick (Inner
    Circle Academy favored, Inner Circle Academy won) to DISPLAY with
    swapped team names ("predicted BRUTE, actual BRUTE") even though
    the underlying win_loss_correct=1 grading was right all along.
    Sourcing team names from the settle's own predictions row instead
    makes the display immune to any later API re-ordering."""
    if not game_ids:
        return {}
    placeholders = ",".join("?" for _ in game_ids)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT s.*, p.home_team AS home_team, p.away_team AS away_team
            FROM settles s
            JOIN predictions p ON p.game_id = s.game_id
            WHERE s.game_id IN ({placeholders})
            """,
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
