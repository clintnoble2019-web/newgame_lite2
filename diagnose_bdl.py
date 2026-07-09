"""
One-off diagnostic — prints the RAW JSON BallDontLie actually returns
for one game, so we can see the real field names instead of guessing.

Run: python diagnose_bdl.py
"""
import sys
sys.path.insert(0, ".")
import json
import config
import requests
from datetime import date, timedelta

print(f"Using BDL_API_KEY: {'set' if config.BDL_API_KEY else 'NOT SET'}")
print(f"Using BDL_SEASON: {config.BDL_SEASON}")
print()

headers = {"Authorization": config.BDL_API_KEY}

# Try today AND yesterday, since today's games may not have scores yet
for days_back in (0, 1, 2):
    d = (date.today() - timedelta(days=days_back)).isoformat()
    print(f"{'='*70}")
    print(f"  Fetching MLB games for {d}")
    print(f"{'='*70}")
    r = requests.get("https://api.balldontlie.io/mlb/v1/games",
                     headers=headers, params={"dates[]": d}, timeout=20)
    print(f"Status code: {r.status_code}")
    if r.status_code != 200:
        print(f"Response text: {r.text[:500]}")
        continue
    data = r.json()
    games = data.get("data", [])
    print(f"Games returned: {len(games)}")
    if games:
        print("\nFULL RAW JSON of first game:")
        print(json.dumps(games[0], indent=2))
        break   # got real data, stop here
    print("(no games this date, trying another)\n")
