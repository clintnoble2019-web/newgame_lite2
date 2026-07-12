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

# ── Simulation (LOCKED decisions — FDD v1.1) ─────────────────────────
SIMULATION_RUNS = 10_000        # locked: 10k, not 1k

TRIM_PCT = 0.30                 # narrowed 2026-07-10: 2.5% (95% CI) read
                                 # as a near-unfailable, unusably wide
                                 # range for customers. 30% trim keeps
                                 # a 40%-window band (30th-70th pctile),
                                 # tight and genuinely predictive.
TRIM_COUNT = int(SIMULATION_RUNS * TRIM_PCT)

# ── Confidence signal thresholds (MARGIN width, redefined 2026-07-11) ─
# Now measured against the trimmed width of (home - away) across all
# iterations, not each team's individual score spread.
CONFIDENCE_BANDS_MLB = {"high": 3, "medium": 5}
CONFIDENCE_BANDS_NBA = {"high": 8, "medium": 14}

# ── Player data sufficiency (LOCKED) ─────────────────────────────────
MLB_PITCHER_MIN_GAMES = 5       # pitchers need 5 starts before rolling avg
MLB_BATTER_MIN_GAMES = 0        # no minimum — any recent data wins
NBA_PLAYER_MIN_GAMES = 0        # no minimum

ROLLING_WINDOW = 15             # last-15-games window everywhere

# ── Injury probability weights (LOCKED) ──────────────────────────────
# Players are NEVER removed from roster — only weighted.
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
LEAGUE_AVG_PPG_WNBA = 81.0      # league-average points per team per game
OT_LENGTH_FACTOR = 5 / 48       # OT possessions = pace * this

# ── Confidence signal thresholds (score range width) ─────────────────
CONFIDENCE_BANDS_MLB = {"high": 4, "medium": 7}    # runs
CONFIDENCE_BANDS_NBA = {"high": 14, "medium": 24}  # points

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
