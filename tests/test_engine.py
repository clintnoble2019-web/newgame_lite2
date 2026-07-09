"""
NexGame Lite — Test Suite
Kage Software · 2026

Every test maps to a LOCKED design decision from the FDD.
Run:  python -m pytest tests/ -v     (or python tests/test_engine.py)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
import unittest

import config
from models import Sport, InjuryStatus, PlayerStats
from ingest.mock_provider import MockProvider
from engine.mlb_sim import simulate_mlb_game, _resolve_pitcher
from engine.nba_sim import simulate_nba_game
from engine.aggregate import run_simulation, _trimmed
from settle.pipeline import settle_player_total, settle_game, \
    check_calibration_signal
from db import database as db

TEST_DB = "test_nexgame.db"
provider = MockProvider()


def mlb_game():
    return provider.get_game_context("MLB_2026-07-08_TOR@SEA", Sport.MLB)


def nba_game():
    return provider.get_game_context("NBA_2026-07-08_MIN@PHX", Sport.NBA)


class TestLockedDecisions(unittest.TestCase):

    # LOCKED: rotation average fallback when no confirmed starter
    def test_rotation_avg_fallback(self):
        game = mlb_game()
        game.home_team.confirmed_starter = None
        p = _resolve_pitcher(game.home_team, random.Random(1))
        self.assertEqual(p.data_source, "team_avg")
        self.assertEqual(p.era, game.home_team.rotation_avg_era)

    # LOCKED: injured players stay on roster
    def test_injured_player_stays_on_roster(self):
        game = mlb_game()
        roster_before = len(game.home_team.roster)
        for p in game.home_team.roster:
            p.injury_status = InjuryStatus.OUT
        simulate_mlb_game(game, random.Random(2))
        self.assertEqual(len(game.home_team.roster), roster_before)

    # LOCKED: trim 2.5% each tail
    def test_trim_2_5_pct(self):
        scores = list(range(config.SIMULATION_RUNS))
        lo, med, hi = _trimmed(scores)
        expected_trim = int(config.SIMULATION_RUNS * config.TRIM_PCT)  # 250
        self.assertEqual(lo, expected_trim)
        self.assertEqual(hi, config.SIMULATION_RUNS - expected_trim - 1)

    # LOCKED: ±20% correctness band
    def test_settle_band(self):
        r = settle_player_total("p1", "Test", "points", 24.3, 26.0)
        self.assertTrue(r.correct)
        self.assertAlmostEqual(r.band_low, 19.4, places=1)
        self.assertAlmostEqual(r.band_high, 29.2, places=1)

        r = settle_player_total("p1", "Test", "points", 24.3, 31.0)
        self.assertFalse(r.correct)
        self.assertEqual(r.direction, "over")

        r = settle_player_total("p1", "Test", "points", 24.3, 15.0)
        self.assertEqual(r.direction, "under")

    # LOCKED: calibration fires only after 15 games, >20% same direction
    def test_calibration_signal(self):
        under_15 = [settle_player_total("p", "T", "pts", 20, 30)
                    for _ in range(10)]
        self.assertEqual(check_calibration_signal(under_15), "ok")

        drifted = [settle_player_total("p", "T", "pts", 20, 30)
                   for _ in range(15)]                 # all 'over'
        self.assertEqual(check_calibration_signal(drifted), "drift_over")

        calibrated = [settle_player_total("p", "T", "pts", 20, 20)
                      for _ in range(15)]
        self.assertEqual(check_calibration_signal(calibrated), "ok")


class TestEngineSanity(unittest.TestCase):
    """Output must look like real baseball / basketball."""

    def test_mlb_scores_realistic(self):
        game = mlb_game()
        rng = random.Random(42)
        scores = [simulate_mlb_game(game, rng) for _ in range(300)]
        avg = sum(r.home_score + r.away_score for r in scores) / len(scores)
        self.assertGreater(avg, 5.0, "MLB total runs too low")
        self.assertLess(avg, 14.0, "MLB total runs too high")
        self.assertTrue(all(r.home_score != r.away_score for r in scores),
                        "no ties allowed")

    def test_nba_scores_realistic(self):
        game = nba_game()
        rng = random.Random(42)
        scores = [simulate_nba_game(game, rng) for _ in range(300)]
        avg = sum(r.home_score + r.away_score for r in scores) / len(scores)
        self.assertGreater(avg, 180, "NBA total points too low")
        self.assertLess(avg, 280, "NBA total points too high")
        self.assertTrue(all(r.home_score != r.away_score for r in scores))

    def test_aggregation_output(self):
        game = mlb_game()
        pred = run_simulation(game, runs=1000, seed=7)
        self.assertAlmostEqual(
            pred.home_win_pct + pred.away_win_pct, 100.0, places=1)
        self.assertLessEqual(pred.score_low_home, pred.score_med_home)
        self.assertLessEqual(pred.score_med_home, pred.score_high_home)
        self.assertIn(pred.confidence, ("high", "medium", "high_variance"))
        self.assertTrue(pred.player_projections)

    def test_reproducible_with_seed(self):
        game = mlb_game()
        a = run_simulation(game, runs=500, seed=99)
        b = run_simulation(game, runs=500, seed=99)
        self.assertEqual(a.home_win_pct, b.home_win_pct)


class TestFullPipeline(unittest.TestCase):

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        db.init_db(TEST_DB)

    def tearDown(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def test_predict_settle_accuracy(self):
        game = mlb_game()
        pred = run_simulation(game, runs=500, seed=3)
        db.save_prediction(pred, TEST_DB)

        box = provider.get_final_boxscore(game.game_id, Sport.MLB)
        result = settle_game(pred, box)
        db.save_settle(result, TEST_DB)

        acc = db.get_accuracy_summary(path=TEST_DB)
        self.assertEqual(acc["total_games"], 1)
        self.assertIn(acc["win_loss_pct"], (0.0, 100.0))
        self.assertGreaterEqual(acc["player_preds_settled"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
