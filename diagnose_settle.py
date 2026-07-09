"""
Diagnostic — checks whether the SINGLE-game lookup endpoint
(games/{game_id}, used specifically by get_final_boxscore for
settling) returns the same field shape as the LIST endpoint
(games?dates[]=...) already confirmed correct for the live ticker.

If they differ, that's exactly why settling could compute the wrong
home/away scores even though the live ticker shows correctly.

Run: python diagnose_settle.py <game_id>
Example: python diagnose_settle.py 5059165
"""
import sys
sys.path.insert(0, ".")
import json
import config
import requests

if len(sys.argv) < 2:
    print("Usage: python diagnose_settle.py <game_id>")
    print("(Find the game_id in the browser URL bar or dev tools network "
         "tab when you clicked 'Settle' — or just check today's games list)")
    sys.exit(1)

game_id = sys.argv[1]
headers = {"Authorization": config.BDL_API_KEY}

print(f"{'='*70}")
print(f"  SINGLE-GAME endpoint: /mlb/v1/games/{game_id}")
print(f"{'='*70}")
r = requests.get(f"https://api.balldontlie.io/mlb/v1/games/{game_id}",
                 headers=headers, timeout=20)
print(f"Status: {r.status_code}")
single_game_data = r.json()
print(json.dumps(single_game_data, indent=2)[:3000])

print(f"\n{'='*70}")
print(f"  What our _score() helper would extract from this shape:")
print(f"{'='*70}")
from ingest.balldontlie_provider import _score
game_obj = single_game_data.get("data", single_game_data)
print(f"home_score extracted: {_score(game_obj, 'home')}")
print(f"away_score extracted: {_score(game_obj, 'away')}")
print(f"home_team: {game_obj.get('home_team_name') or game_obj.get('home_team', {}).get('display_name')}")
print(f"away_team: {game_obj.get('away_team_name') or game_obj.get('away_team', {}).get('display_name')}")

print(f"\n{'='*70}")
print(f"  STATS endpoint (player box score) for comparison:")
print(f"{'='*70}")
r2 = requests.get("https://api.balldontlie.io/mlb/v1/stats",
                  headers=headers, params={"game_ids[]": game_id}, timeout=20)
stats_data = r2.json()
rows = stats_data.get("data", [])
print(f"Rows returned: {len(rows)}")
if rows:
    print("First row sample:")
    print(json.dumps(rows[0], indent=2)[:1500])
