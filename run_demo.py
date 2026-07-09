"""
NexGame Lite — End-to-End Demo Runner
Kage Software · 2026

Runs the FULL pipeline in the terminal, no dashboard needed:
    ingest -> simulate (10,000x) -> predict -> settle -> accuracy log

Usage:
    python run_demo.py                # both sports, today's mock slate
    python run_demo.py --runs 2000    # faster test run
"""

import argparse
import time
from datetime import date

import config
from models import Sport
from ingest.base import get_provider
from engine.aggregate import run_simulation
from settle.pipeline import settle_game
from db import database as db


def demo_game(provider, game, runs):
    print(f"\n{'='*64}")
    print(f"  {game.away_team.name} @ {game.home_team.name}"
          f"  ({game.sport.value})")
    print(f"  {game.game_date} · {game.game_time} · {game.venue}")

    sp = game.home_team.confirmed_starter
    print(f"  Home SP: {sp.name + f' (ERA {sp.era})' if sp else 'NOT CONFIRMED -> Rotation Avg'}")

    # ── simulate ─────────────────────────────────────────────────────
    t0 = time.time()
    pred = run_simulation(game, runs=runs)
    elapsed = time.time() - t0
    print(f"\n  Ran {pred.simulations_run:,} simulations in {elapsed:.1f}s")

    print(f"\n  WIN PROBABILITY")
    print(f"    {pred.home_team:<28} {pred.home_win_pct}%")
    print(f"    {pred.away_team:<28} {pred.away_win_pct}%")

    print(f"\n  SCORE RANGE (trimmed 95% window)")
    print(f"    {pred.home_team:<28} {pred.score_low_home}–"
          f"{pred.score_high_home}  (median {pred.score_med_home})")
    print(f"    {pred.away_team:<28} {pred.score_low_away}–"
          f"{pred.score_high_away}  (median {pred.score_med_away})")
    print(f"    Confidence: {pred.confidence}")

    print(f"\n  TOP PLAYER PROJECTIONS")
    metric = "points" if game.sport == Sport.NBA else "hits"
    top = sorted(pred.player_projections.items(),
                 key=lambda kv: kv[1].get(metric, 0), reverse=True)[:5]
    for pid, proj in top:
        stats = ", ".join(f"{k}: {v}" for k, v in proj.items() if k != "name")
        print(f"    {proj['name']:<26} {stats}")

    db.save_prediction(pred)

    # ── settle ───────────────────────────────────────────────────────
    box = provider.get_final_boxscore(game.game_id, game.sport)
    result = settle_game(pred, box)
    db.save_settle(result)

    wl = "✅" if result.win_loss_correct else "❌"
    sr = "✅" if result.score_range_correct else "❌"
    print(f"\n  SETTLED — Final: {result.actual_away}–{result.actual_home}")
    print(f"    {wl} Win/Loss   (predicted {result.predicted_winner}, "
          f"actual {result.actual_winner})")
    print(f"    {sr} Score Range")
    print(f"    Player totals: {result.player_accuracy_pct}% within ±20% band")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=config.SIMULATION_RUNS,
                    help="simulations per game (default from config)")
    ap.add_argument("--date", type=str,
                    default=date.today().strftime("%Y-%m-%d"))
    args = ap.parse_args()

    print(f"NexGame Lite — provider: {config.DATA_PROVIDER} · "
          f"runs: {args.runs:,}")
    db.init_db()
    provider = get_provider()

    for sport in (Sport.MLB, Sport.NBA):
        games = provider.get_games_for_date(sport, args.date)
        print(f"\n{sport.value}: {len(games)} games on {args.date}")
        for game in games[:2]:              # first 2 per sport for the demo
            demo_game(provider, game, args.runs)

    # ── accuracy summary ─────────────────────────────────────────────
    print(f"\n{'='*64}")
    print("  ACCURACY LOG (all-time)")
    acc = db.get_accuracy_summary()
    print(f"    Settled games:        {acc['total_games']}")
    print(f"    Win/Loss accuracy:    {acc['win_loss_pct']}%")
    print(f"    Score range accuracy: {acc['score_range_pct']}%")
    print(f"    Player total accuracy:{acc['player_total_pct']}% "
          f"({acc['player_preds_settled']} predictions)")
    print(f"{'='*64}\n")


if __name__ == "__main__":
    main()
