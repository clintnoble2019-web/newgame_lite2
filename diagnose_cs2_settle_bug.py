"""
diagnose_cs2_settle_bug.py — pull the raw predictions + settles rows
for the Inner Circle Academy @ BRUTE match to see exactly what got
stored: does home_team/away_team match home_win_pct/away_win_pct the
way the UI displayed it, and does predicted_winner in the settle row
actually correspond to the higher win_pct at prediction time?

Run from repo root: python diagnose_cs2_settle_bug.py
Paste the FULL output back.
"""
import sqlite3
import json
import config

conn = sqlite3.connect(config.DB_PATH)
conn.row_factory = sqlite3.Row

print("=== Searching predictions for Inner Circle Academy / BRUTE ===")
rows = conn.execute(
    "SELECT * FROM predictions WHERE home_team LIKE '%BRUTE%' "
    "OR away_team LIKE '%BRUTE%' OR home_team LIKE '%Inner Circle%' "
    "OR away_team LIKE '%Inner Circle%'"
).fetchall()
for r in rows:
    d = dict(r)
    print(f"\ngame_id={d['game_id']} sport={d['sport']}")
    print(f"  home_team={d['home_team']!r}  away_team={d['away_team']!r}")
    print(f"  home_win_pct={d['home_win_pct']}  away_win_pct={d['away_win_pct']}")
    print(f"  score_med_home={d['score_med_home']}  score_med_away={d['score_med_away']}")
    print(f"  generated_at={d['generated_at']}  stored_at={d['stored_at']}")

print("\n=== Searching settles for the same game_ids ===")
for r in rows:
    gid = dict(r)["game_id"]
    s = conn.execute(
        "SELECT * FROM settles WHERE game_id = ?", (gid,)
    ).fetchone()
    if s:
        sd = dict(s)
        print(f"\ngame_id={gid}")
        print(f"  predicted_winner={sd['predicted_winner']!r}  "
             f"actual_winner={sd['actual_winner']!r}")
        print(f"  win_loss_correct={sd['win_loss_correct']}")
        print(f"  actual_home={sd['actual_home']}  actual_away={sd['actual_away']}")
        print(f"  settled_at={sd['settled_at']}")
    else:
        print(f"\ngame_id={gid}: no settle row found")

conn.close()
print("\n\nDone — paste everything above back into the chat.")
