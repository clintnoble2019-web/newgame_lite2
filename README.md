# NexGame Lite

**Kage Software · 2026 · CONFIDENTIAL**

Sports prediction platform — 10,000 Monte Carlo simulations per game.
MLB + NBA. Win/Loss · Score Range · Player Totals.

Proof of concept + $450 lifetime Contra B2B offering. Launch: **August 25, 2026**.

---

## Quick Start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Full pipeline demo in the terminal (fast test with 2,000 runs)
python run_demo.py --runs 2000

# 3. Full 10,000-run demo (the locked production number)
python run_demo.py

# 4. Launch the dashboard
uvicorn api.main:app --reload
# -> open http://localhost:8000

# 5. Run tests (every test maps to a LOCKED FDD decision)
python tests/test_engine.py
```

## The API Swap (config.py)

```python
DATA_PROVIDER = "mock"            # now: testing, zero internet needed
DATA_PROVIDER = "free"            # dev: MLB Stats API + BallDontLie free tier
DATA_PROVIDER = "balldontlie"     # PRIMARY: GOAT tier, $79.98/mo, both sports
DATA_PROVIDER = "mysportsfeeds"   # backup option, $150/mo, kept as fallback
```

One line. Nothing else in the codebase changes. All API-specific parsing
lives inside `ingest/` — the engine, settling, and dashboard never touch
raw API responses.

**Before flipping to `balldontlie` for real launch traffic:** run one
real game through `get_game_context()` and `get_final_boxscore()`,
print the raw JSON, and diff it against `balldontlie_provider.py`.
A few endpoint shapes (NBA season-averages category/type pairing, the
exact MLB "starting pitcher" lineup field) are built from documented
examples, not a live test call — marked with `VERIFY` comments in the
file. Everything else is confirmed against real BallDontLie response
shapes.

Set `BDL_API_KEY` via the `BDL_API_KEY` environment variable (Railway:
add it under Variables — see `DEPLOYMENT.md`).

## Project Structure

```
nexgame_lite/
├── config.py              # ALL locked decisions as constants + API swap
├── models.py              # Dataclasses (pitcher 0.00 / batter 0.000)
├── run_demo.py            # Full pipeline, end to end, in terminal
├── ingest/
│   ├── base.py            # DataProvider interface + factory (swap point)
│   ├── mock_provider.py   # Realistic generated data (testing)
│   ├── free_provider.py   # MLB Stats API + BallDontLie (dev only)
│   └── msf_provider.py    # MySportsFeeds (production, Aug 25)
├── engine/
│   ├── mlb_sim.py         # Inning-by-inning · fatigue · base state
│   ├── nba_sim.py         # Quarter-by-quarter · possessions · fouls
│   └── aggregate.py       # 10,000 runs -> trimmed range + confidence
├── settle/
│   └── pipeline.py        # ±20% band · drift detection (mom audits this)
├── db/
│   └── database.py        # SQLite (Module 3 SQL) -> Postgres at full
├── api/
│   ├── main.py            # FastAPI backend (carries into Phase 1)
│   └── static/index.html  # Dashboard — HTML/JS, dark theme
└── tests/
    └── test_engine.py     # Locked decisions as tests
```

## Locked Decisions in Code

| Decision | Where |
|---|---|
| 10,000 simulations | `config.SIMULATION_RUNS` |
| Trim 2.5% each tail | `engine/aggregate.py::_trimmed` |
| Inning-by-inning / quarter-by-quarter | `engine/mlb_sim.py` / `engine/nba_sim.py` |
| Garbage time simulated normally | (no modifier exists — by design) |
| Rotation avg fallback (bullpen excluded) | `engine/mlb_sim.py::_resolve_pitcher` |
| Bullpen always separate | `engine/mlb_sim.py::_bullpen_pitcher` |
| Injury weights, players never removed | `config.INJURY_WEIGHTS` + both engines |
| Pitcher 5-game min / no batter or NBA min | `config` + `ingest/free_provider.py` |
| Fallback chain: recent → career → team avg | `ingest/` providers |
| ±20% settle band | `settle/pipeline.py::settle_player_total` |
| Drift signal: 15 games, >20% same direction | `settle/pipeline.py::check_calibration_signal` |
| Pitcher stats 0.00 / batter stats 0.000 | `models.py::PlayerStats` |

## Refinement Plan (Bootcamp Modules)

- **Module 1 (Data):** validate real API response shapes in `free_provider.py`
- **Module 2 (Analytics):** tune engine constants against Playvo validation data
- **Module 3 (SQL):** extend the schema in `db/database.py` — same SQL as class
- **Module 4 (Python):** production hardening; fill `msf_provider.py` parsers
- **Aug 25:** flip `DATA_PROVIDER = "mysportsfeeds"` → launch

## Customer Management — Two Buyer Populations

**B2B (Contra, direct):** Season ($450) / Lifetime ($900) — manual entry,
you add each sale by hand.

**B2C (Gumroad, Discover):** Monthly Basic ($19.99) / Pro ($39.99) —
auto-tracked via a Gumroad webhook. Matches full NexGame's locked
subscription pricing exactly, so today's Lite subscriber already knows
the price when they graduate to full NexGame in June 2027.

```bash
# B2B: after a Contra sale comes through
python manage_customers.py add

# See everyone, or filter by population:
python manage_customers.py list
python manage_customers.py list --b2b
python manage_customers.py list --b2c

# Preview who needs a message right now (safe — sends nothing):
python manage_customers.py check
python manage_customers.py check --send      # mark as contacted

# Manually move a B2C subscriber Basic -> Pro:
python manage_customers.py upgrade <customer_id> pro
```

**The full decision tree** (`customers.py::determine_action`):

| Population | Condition | Action |
|---|---|---|
| B2B Season | season active | nothing |
| B2B Season | <14 days left | `SEASON_ENDING_SOON` |
| B2B Season | season ended | `RENEWAL_PLEA` |
| B2B Lifetime | new season released | `LIFETIME_THANKS` |
| B2C Basic | active, <60 days tenure | nothing |
| B2C Basic | active, ≥60 days tenure | `UPGRADE_NUDGE` |
| B2C Pro | active, any tenure | nothing (already top tier) |
| B2C any | cancelled ≤3 days ago | `WIN_BACK` (sent once per cancellation) |
| B2C any | cancelled >3 days ago | nothing (window missed, don't nag) |

`SEASON_END`, `UPGRADE_NUDGE_AFTER_DAYS`, and `WIN_BACK_WINDOW_DAYS` in
`customers.py` are the tunable constants everything else derives from.

### Gumroad Webhook (auto-tracks B2C status)

`gumroad_webhook.py` receives Gumroad's Ping events at `/webhooks/gumroad`
and keeps `sub_status` live — no manual entry needed for Monthly tiers.

**Setup at launch:**
1. Deploy `api/main.py` (webhook router is already wired in)
2. In Gumroad: Settings → Advanced → Ping endpoint → paste your server's
   `/webhooks/gumroad` URL
3. In the Gumroad product editor, name your variants **exactly**
   `Basic — $19.99/mo` and `Pro — $39.99/mo` (must match
   `gumroad_webhook.py::VARIANT_TIER_MAP`) or update the map to match
   whatever you actually name them
4. Set `GUMROAD_WEBHOOK_SECRET` in `config.py` once Gumroad shows you
   the signing secret (Settings → Advanced)

## Costs (locked)

- BallDontLie GOAT tier (MLB + NBA): **$79.98/mo** — primary provider
- AWS/Railway hosting: **~$10-25/mo**
- **Total operating cost: ~$90-105/mo** (down from ~$175/mo with MySportsFeeds)
- Season sale ($450) covers 4+ months. Lifetime sale ($900) covers 8+ months.
- First sale each month covers that month's cost — everything after is margin.
