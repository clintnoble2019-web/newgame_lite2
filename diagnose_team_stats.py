"""
Diagnostic — checks whether team-level season stats (OBP/SLG/ERA) are
actually populating from BallDontLie, or silently failing and leaving
every team at default values (which would explain near-identical win%
across every matchup regardless of real team strength).

Run: python diagnose_team_stats.py
"""
import sys
sys.path.insert(0, ".")
import config
import requests
import json

print(f"BDL_API_KEY set: {bool(config.BDL_API_KEY)}")
headers = {"Authorization": config.BDL_API_KEY}

# Try a handful of plausible endpoint paths for MLB team season stats
candidates = [
    "season_stats",
    "team_season_averages",
    "teams/season_stats",
    "season_averages",
]

print("\n--- Testing candidate team-stats endpoints (MLB) ---")
for path in candidates:
    url = f"https://api.balldontlie.io/mlb/v1/{path}"
    try:
        r = requests.get(url, headers=headers,
                         params={"season": 2026, "team_ids[]": 28},  # Rangers
                         timeout=15)
        print(f"\n{path}: status={r.status_code}")
        if r.status_code == 200:
            data = r.json()
            rows = data.get("data", [])
            print(f"  rows returned: {len(rows)}")
            if rows:
                print(f"  sample keys: {list(rows[0].keys())[:15]}")
                print(f"  batting_obp: {rows[0].get('batting_obp')}")
                print(f"  pitching_era: {rows[0].get('pitching_era')}")
        else:
            print(f"  body: {r.text[:200]}")
    except Exception as e:
        print(f"{path}: ERROR — {e}")

print("\n--- Now checking what a real GameContext actually has ---")
config.DATA_PROVIDER = "balldontlie"
from ingest.balldontlie_provider import BallDontLieProvider
from models import Sport
from datetime import date

p = BallDontLieProvider()
games = p.get_games_for_date(Sport.MLB, date.today().isoformat())
if games:
    ctx = p.get_game_context(games[0].game_id, Sport.MLB)
    print(f"\nGame: {ctx.away_team.name} @ {ctx.home_team.name}")
    print(f"Home team_obp: {ctx.home_team.team_obp}")
    print(f"Home team_slg: {ctx.home_team.team_slg}")
    print(f"Home rotation_avg_era: {ctx.home_team.rotation_avg_era}")
    print(f"Away team_obp: {ctx.away_team.team_obp}")
    print(f"Away team_slg: {ctx.away_team.team_slg}")
    print(f"Away rotation_avg_era: {ctx.away_team.rotation_avg_era}")
    if ctx.home_team.team_obp == 0 and ctx.away_team.team_obp == 0:
        print("\n*** CONFIRMED: team stats are NOT populating — both at 0 ***")
else:
    print("No games found for today.")
