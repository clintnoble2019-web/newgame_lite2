"""
NexGame Lite — Configuration
Kage Software · 2026

THE API SWAP LIVES HERE.
    Development/testing:  DATA_PROVIDER = "mock" or "mlb_free" / "balldontlie"
    Public release Aug 25: DATA_PROVIDER = "mysportsfeeds"
One line change. Nothing else in the codebase touches API specifics.
"""

import os

# ── Data Provider ────────────────────────────────────────────────────
# Options: "mock" | "free" | "balldontlie" | "mysportsfeeds"
#   mock          → generated realistic data, no internet needed (testing)
#   free          → MLB Stats API + BallDontLie NBA (dev only, NOT for release)
#   balldontlie   → production, GOAT tier both sports ($79.98/mo locked)
#   mysportsfeeds → alternate production option ($150/mo, kept as backup)
DATA_PROVIDER = "balldontlie"

# MySportsFeeds credentials (backup option, fill in if used)
MSF_API_KEY = ""
MSF_PASSWORD = ""
MSF_SEASON = "2026-regular"

# BallDontLie credentials (PRIMARY — GOAT tier, MLB + NBA, $79.98/mo locked)
BDL_API_KEY = os.environ.get("BDL_API_KEY", "")
BDL_SEASON = 2026   # int, not string — BallDontLie uses bare year

# ── AI Read Generation (added 2026-07-16) ────────────────────────────
# Translates a stored SimulationOutput into a short natural-language
# "read" — the on-camera talking-point version of the numbers, used
# for the daily pick videos. Generated once per game, cached in the
# predictions table (read_text column) — never regenerated on repeat
# views, since the underlying prediction is locked once stored.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
READ_MODEL = "claude-haiku-4-5-20251001"   # fluent translation task,
                                           # doesn't need heavy reasoning

# ── Simulation (LOCKED decisions — FDD v1.1) ─────────────────────────
SIMULATION_RUNS = 10_000        # locked: 10k, not 1k

TRIM_PCT = 0.30                 # narrowed 2026-07-10: 2.5% (95% CI) read
                                 # as a near-unfailable, unusably wide
                                 # range for customers. 30% trim keeps
                                 # a 40%-window band (30th-70th pctile),
                                 # tight and genuinely predictive.
TRIM_COUNT = int(SIMULATION_RUNS * TRIM_PCT)

# ── Player data sufficiency (LOCKED) ─────────────────────────────────
MLB_PITCHER_MIN_GAMES = 5       # pitchers need 5 starts before rolling avg
MLB_BATTER_MIN_GAMES = 0        # no minimum — any recent data wins
NBA_PLAYER_MIN_GAMES = 0        # no minimum

ROLLING_WINDOW = 15             # last-15-games window everywhere

# ── Injury probability weights (LOCKED) ──────────────────────────────
# Players are NEVER removed from roster — only weighted.
# Rotation construction (added for efficiency-vs-minutes upgrade,
# 2026-07-14): real NBA/WNBA teams rarely give meaningful minutes
# beyond ~10 players — capping the sim's rotation the same way stops
# deep-bench players from diluting the scoring-credit pool on equal
# structural footing with starters.
NBA_ROTATION_SIZE = 10
# League-average true shooting %, used to weight scoring credit by
# efficiency (not just usage/minutes) — matches the fallback default
# already used when hydrating a player's true_shooting.
LEAGUE_AVG_TS = 0.560
# Fallback minutes_proj when a player's hydrated value is 0/missing —
# needed so the minutes-ranking sort has something sane to work with.
PLAYER_MIN_MINUTES_DEFAULT = 15.0

INJURY_WEIGHTS = {
    "active":       {"plays": 1.00, "capacity": 1.00},
    "probable":     {"plays": 0.85, "capacity": 0.90},
    "questionable": {"plays": 0.60, "capacity": 0.85},
    "out":          {"plays": 0.00, "capacity": 0.00},
    "ir":           {"plays": 0.00, "capacity": 0.00},
}

# ── Settling (LOCKED) ────────────────────────────────────────────────
CORRECTNESS_THRESHOLD = 0.20    # ±20% band for player totals
CALIBRATION_WINDOW = 15         # games before drift check fires
DRIFT_THRESHOLD = 0.20          # >20% misses same direction = drift flag

# ── MLB engine tuning constants ──────────────────────────────────────
LEAGUE_AVG_OBP = 0.320
LEAGUE_AVG_WHIP = 1.30
LEAGUE_AVG_ERA = 4.20
PITCH_COUNT_PULL = 95           # starter pulled around this pitch count
PITCHES_PER_PA = 3.9            # league average pitches per plate appearance

# Hit type distribution baseline (tuned by player SLG in engine)
HIT_TYPE_BASE = {"single": 0.66, "double": 0.20, "triple": 0.02, "hr": 0.12}

# ── NBA engine tuning constants ──────────────────────────────────────
LEAGUE_AVG_ORTG = 113.0
LEAGUE_AVG_DRTG = 113.0
LEAGUE_AVG_PACE = 99.0          # possessions per team per 48 min

# WNBA equivalents (40-minute games, lower scoring environment).
# Used as fallbacks + PPP normalization baseline when sport == WNBA.
# PACE is possessions per team per 40-min game (raw, ~80), NOT the
# per-48-normalized figure some tools publish (~96). VERIFY on first
# live pull: check the magnitude of BDL's WNBA team pace — if hydrated
# values come back ~96+ they're per-48-normalized and the sim's
# regulation scaling needs a convention flag.
LEAGUE_AVG_ORTG_WNBA = 101.0
LEAGUE_AVG_DRTG_WNBA = 101.0
LEAGUE_AVG_PACE_WNBA = 80.0     # possessions per team per 40 min (raw)
LEAGUE_AVG_PPG_WNBA = 81.0      # points per team per game — baseline
                                # for deriving team ORtg/DRtg from real
                                # points scored/allowed (WNBA has no
                                # advanced-stats endpoint)
OT_LENGTH_FACTOR = 5 / 48       # OT possessions = pace * this

# ── CS2 engine tuning constants ───────────────────────────────────────
# CS2 has no advanced-stats/season-averages endpoint (same limitation
# as WNBA) — team strength is derived from round win% across the
# team's own finished maps within the SAME tournament as the match
# being predicted (verified-available scope; see provider). Odds
# markets are map-level (Map Handicap, Total Maps O/U), not round-
# level, so the sim simulates map wins via log5, not individual rounds.
CS2_LEAGUE_AVG_ROUND_WIN_PCT = 0.500   # used when a team has no
                                        # finished maps yet in-tournament
CS2_MIN_MAPS_SAMPLE = 3                # below this, blend toward league
                                        # avg rather than trust the raw
                                        # in-tournament sample

# ── Confidence signal thresholds (score range width) ─────────────────
CONFIDENCE_BANDS_MLB = {"high": 4, "medium": 7}    # runs
CONFIDENCE_BANDS_NBA = {"high": 14, "medium": 24}  # points
# CS2 home/away score = MAPS won (e.g. 2-0, 2-1 in a Bo3), a tiny
# integer range — width is measured in maps, not rounds.
CONFIDENCE_BANDS_CS2 = {"high": 1, "medium": 2}

# ── Database ─────────────────────────────────────────────────────────
# SQLite for Lite (zero setup, same SQL you learn in Module 3).
# Production full NexGame: PostgreSQL (schema is compatible).
# On Railway: set DB_PATH env var to the mounted volume path
# (see DEPLOYMENT.md) or predictions/settles vanish on every redeploy.
DB_PATH = os.environ.get("DB_PATH", "nexgame_lite.db")

# ── Dashboard ────────────────────────────────────────────────────────
LIVE_POLL_SECONDS = 60          # live score ticker refresh
TOP_PLAYERS_SHOWN = 5           # top N players per team in prediction view

# ── Gumroad (B2C billing) ─────────────────────────────────────────────
# HMAC secret Gumroad signs webhook pings with — set once you configure
# the Ping endpoint in Gumroad Settings > Advanced.
GUMROAD_WEBHOOK_SECRET = ""

# ── Auth ─────────────────────────────────────────────────────────────
# Signs session cookies. MUST be set to a real random value before
# public launch — a hardcoded default here would let anyone forge a
# login. Generate one with: python -c "import secrets; print(secrets.token_hex(32))"
# On Railway/Render, set this as an environment variable, not in this file.
SECRET_KEY = os.environ.get("NEXGAME_SECRET_KEY", "")
