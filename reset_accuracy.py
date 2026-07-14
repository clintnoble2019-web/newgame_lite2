"""
reset_accuracy.py — WIPES the predictions and settles tables entirely.
IRREVERSIBLE. Only run this after reviewing preview_accuracy_reset.py's
output and confirming you want a full, all-sports reset.

Run: python reset_accuracy.py
"""
import sqlite3
import config

conn = sqlite3.connect(config.DB_PATH)

pred_before = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
settle_before = conn.execute("SELECT COUNT(*) FROM settles").fetchone()[0]

conn.execute("DELETE FROM settles")
conn.execute("DELETE FROM predictions")
conn.commit()

pred_after = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
settle_after = conn.execute("SELECT COUNT(*) FROM settles").fetchone()[0]

print(f"predictions: {pred_before} -> {pred_after}")
print(f"settles:     {settle_before} -> {settle_after}")
print("\nDone. Accuracy tab and Games tab will start clean from the "
     "next prediction/settle cycle.")

conn.close()
