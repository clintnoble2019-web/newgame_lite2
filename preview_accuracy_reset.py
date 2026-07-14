"""
preview_accuracy_reset.py — shows exactly what a full accuracy reset
would delete, BEFORE anything is actually removed. Run this first.

Run: python preview_accuracy_reset.py
"""
import sqlite3
import config

conn = sqlite3.connect(config.DB_PATH)
conn.row_factory = sqlite3.Row

pred_count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
settle_count = conn.execute("SELECT COUNT(*) FROM settles").fetchone()[0]

print(f"predictions table: {pred_count} rows")
print(f"settles table:     {settle_count} rows")

print("\nBy sport (predictions):")
for row in conn.execute(
        "SELECT sport, COUNT(*) as n FROM predictions GROUP BY sport"):
    print(f"  {row['sport']}: {row['n']}")

print("\nBy sport (settles):")
for row in conn.execute(
        "SELECT sport, COUNT(*) as n FROM settles GROUP BY sport"):
    print(f"  {row['sport']}: {row['n']}")

print(f"\nThis would be a FULL reset — both tables emptied, every sport, "
     f"{pred_count} predictions + {settle_count} settles deleted. "
     f"Nothing removed yet — this is preview only.")

conn.close()
