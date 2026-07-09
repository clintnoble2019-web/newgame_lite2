"""
NexGame Lite — Mock Data Provider
Kage Software · 2026

Generates realistic MLB and NBA data so the ENTIRE pipeline
(ingest -> simulate -> dashboard -> settle) can be tested end-to-end
with zero internet and zero API keys.

Player stats are sampled from realistic league distributions, so
simulation output should look like real baseball / basketball.
"""

import random
import hashlib
from datetime import datetime
from ingest.base import DataProvider
from models import (
    GameContext, PlayerStats, TeamData,
    Sport, GameStatus, InjuryStatus,
)

# Fictional teams — real team names/marks not used in mock data
MLB_TEAMS = [
    ("NYY", "New York Yankees"), ("LAD", "Los Angeles Dodgers"),
    ("BOS", "Boston Red Sox"), ("SFG", "San Francisco Giants"),
    ("CHC", "Chicago Cubs"), ("ATL", "Atlanta Braves"),
    ("HOU", "Houston Astros"), ("PHI", "Philadelphia Phillies"),
    ("SDP", "San Diego Padres"), ("TEX", "Texas Rangers"),
    ("SEA", "Seattle Mariners"), ("TOR", "Toronto Blue Jays"),
]
NBA_TEAMS = [
    ("LAL", "Los Angeles Lakers"), ("BOS", "Boston Celtics"),
    ("GSW", "Golden State Warriors"), ("MIL", "Milwaukee Bucks"),
    ("PHX", "Phoenix Suns"), ("DEN", "Denver Nuggets"),
    ("MIA", "Miami Heat"), ("NYK", "New York Knicks"),
    ("DAL", "Dallas Mavericks"), ("PHI", "Philadelphia 76ers"),
    ("OKC", "Oklahoma City Thunder"), ("MIN", "Minnesota Timberwolves"),
]

FIRST = ["Marcus", "Tyler", "Jalen", "Diego", "Kenta", "Andre", "Luis",
         "Trevor", "Malik", "Jordan", "Casey", "Devon", "Rafael", "Isaiah"]
LAST = ["Rivera", "Brooks", "Tanaka", "Okafor", "Delgado", "Hayes",
        "Whitfield", "Moreno", "Castellanos", "Grant", "Bishop", "Vaughn"]


def _rng_for(seed_text: str) -> random.Random:
    """Deterministic RNG per game/team so mock data is stable across calls."""
    h = int(hashlib.md5(seed_text.encode()).hexdigest(), 16) % (2**32)
    return random.Random(h)


def _name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST)} {rng.choice(LAST)}"


def _injury(rng: random.Random) -> InjuryStatus:
    roll = rng.random()
    if roll < 0.86:
        return InjuryStatus.ACTIVE
    if roll < 0.92:
        return InjuryStatus.PROBABLE
    if roll < 0.97:
        return InjuryStatus.QUESTIONABLE
    return InjuryStatus.OUT


# ── MLB roster generation ────────────────────────────────────────────
def _mlb_team(team_id: str, name: str, seed: str) -> TeamData:
    rng = _rng_for(seed + team_id)
    team = TeamData(team_id=team_id, name=name, abbrev=team_id, sport=Sport.MLB)

    # Starting pitcher (85% confirmed — tests the rotation-avg fallback)
    starters = []
    for i in range(5):
        p = PlayerStats(
            player_id=f"{team_id}_SP{i}", name=_name(rng), sport=Sport.MLB,
            is_pitcher=True, is_starter=True,
            games_played=rng.randint(3, 20),           # tests 5-game minimum
            era=round(rng.uniform(2.80, 5.40), 2),     # pitcher: 0.00
            whip=round(rng.uniform(1.05, 1.55), 2),
            k_per_9=round(rng.uniform(6.5, 11.5), 2),
            bb_per_9=round(rng.uniform(1.8, 4.2), 2),
            hr_per_9=round(rng.uniform(0.7, 1.8), 2),
            rest_days=rng.randint(4, 6),
            injury_status=_injury(rng),
        )
        starters.append(p)
        team.roster.append(p)

    if rng.random() < 0.85:
        healthy = [s for s in starters
                   if s.injury_status not in (InjuryStatus.OUT, InjuryStatus.IR)]
        if healthy:
            team.confirmed_starter = rng.choice(healthy)

    # Rotation averages (starters ONLY — bullpen excluded, LOCKED)
    team.rotation_avg_era = round(sum(s.era for s in starters) / len(starters), 2)
    team.rotation_avg_whip = round(sum(s.whip for s in starters) / len(starters), 2)
    team.rotation_avg_k9 = round(sum(s.k_per_9 for s in starters) / len(starters), 2)

    # Bullpen — always separate (LOCKED)
    team.bullpen_era = round(rng.uniform(3.20, 4.80), 2)
    team.bullpen_whip = round(rng.uniform(1.15, 1.45), 2)

    # Batting lineup 1-9 — batter: 0.000
    obps = []
    for spot in range(1, 10):
        b = PlayerStats(
            player_id=f"{team_id}_B{spot}", name=_name(rng), sport=Sport.MLB,
            games_played=rng.randint(0, 80),           # 0 tests career fallback
            obp=round(rng.uniform(0.280, 0.400), 3),
            slg=round(rng.uniform(0.340, 0.560), 3),
            babip=round(rng.uniform(0.260, 0.340), 3),
            lineup_spot=spot,
            injury_status=_injury(rng),
        )
        obps.append(b.obp)
        team.roster.append(b)

    team.team_obp = round(sum(obps) / len(obps), 3)
    team.team_slg = 0.430
    return team


# ── NBA roster generation ────────────────────────────────────────────
def _nba_team(team_id: str, name: str, seed: str) -> TeamData:
    rng = _rng_for(seed + team_id)
    team = TeamData(team_id=team_id, name=name, abbrev=team_id, sport=Sport.NBA)

    team.team_ortg = round(rng.uniform(107.0, 119.0), 1)
    team.team_drtg = round(rng.uniform(107.0, 119.0), 1)
    team.team_pace = round(rng.uniform(95.0, 103.0), 1)

    # 5 starters + 5 bench — usage/minutes shaped like a real rotation
    profiles = [
        (True, 34, 24.0), (True, 34, 20.0), (True, 32, 17.0),
        (True, 30, 13.0), (True, 28, 10.0),
        (False, 24, 9.0), (False, 20, 7.0), (False, 16, 6.0),
        (False, 12, 4.0), (False, 10, 3.0),
    ]
    for i, (is_start, mins, base_ppg) in enumerate(profiles):
        p = PlayerStats(
            player_id=f"{team_id}_P{i}", name=_name(rng), sport=Sport.NBA,
            games_played=rng.randint(0, 60),
            ppg=round(base_ppg * rng.uniform(0.8, 1.25), 1),
            apg=round(rng.uniform(1.0, 8.5), 1),
            rpg=round(rng.uniform(2.0, 10.5), 1),
            usage_rate=round(rng.uniform(0.14, 0.32), 3),
            true_shooting=round(rng.uniform(0.520, 0.640), 3),
            minutes_proj=mins,
            is_starter_nba=is_start,
            injury_status=_injury(rng),
        )
        team.roster.append(p)
    return team


# ── Provider ──────────────────────────────────────────────────────────
class MockProvider(DataProvider):
    """Realistic generated data. Deterministic per (date, matchup)."""

    def get_games_for_date(self, sport: Sport, date_str: str) -> list[GameContext]:
        teams = MLB_TEAMS if sport == Sport.MLB else NBA_TEAMS
        rng = _rng_for(f"slate_{sport.value}_{date_str}")
        pairs = teams[:]
        rng.shuffle(pairs)
        games = []
        for i in range(0, len(pairs) - 1, 2):
            away, home = pairs[i], pairs[i + 1]
            gid = f"{sport.value}_{date_str}_{away[0]}@{home[0]}"
            games.append(self.get_game_context(gid, sport))
        return games

    def get_game_context(self, game_id: str, sport: Sport) -> GameContext:
        # game_id format: MLB_2026-07-08_BRV@LAK
        parts = game_id.split("_")
        date_str, matchup = parts[1], parts[2]
        away_id, home_id = matchup.split("@")
        team_lookup = dict(MLB_TEAMS if sport == Sport.MLB else NBA_TEAMS)
        seed = f"{game_id}"

        build = _mlb_team if sport == Sport.MLB else _nba_team
        home = build(home_id, team_lookup[home_id], seed)
        away = build(away_id, team_lookup[away_id], seed)

        rng = _rng_for(seed + "_ctx")
        return GameContext(
            game_id=game_id, sport=sport, status=GameStatus.SCHEDULED,
            home_team=home, away_team=away,
            game_date=date_str,
            game_time=f"{rng.randint(1, 7) + 12}:{rng.choice(['05','10','35','40'])} PT",
            venue=f"{home.name} Park" if sport == Sport.MLB else f"{home.name} Arena",
            park_factor=round(rng.uniform(0.94, 1.08), 2),
        )

    def get_live_scores(self, sport: Sport) -> list[dict]:
        """Simulated live slate for the ticker."""
        date_str = datetime.now().strftime("%Y-%m-%d")
        rng = _rng_for(f"live_{sport.value}_{date_str}_{datetime.now().hour}")
        games = self.get_games_for_date(sport, date_str)
        out = []
        for g in games:
            state = rng.choice(["scheduled", "live", "live", "final"])
            if sport == Sport.MLB:
                h, a = rng.randint(0, 9), rng.randint(0, 9)
                period = f"{rng.choice(['Top', 'Bot'])} {rng.randint(1, 9)}"
            else:
                h, a = rng.randint(20, 125), rng.randint(20, 125)
                period = f"Q{rng.randint(1, 4)}"
            out.append({
                "game_id": g.game_id, "status": state,
                "home": g.home_team.abbrev, "away": g.away_team.abbrev,
                "home_score": h if state != "scheduled" else 0,
                "away_score": a if state != "scheduled" else 0,
                "period": period if state == "live" else state.upper(),
            })
        return out

    def get_final_boxscore(self, game_id: str, sport: Sport) -> dict:
        """Fake final result — lets the settling pipeline run end-to-end.
        Correlated with team quality so settle results are meaningful."""
        ctx = self.get_game_context(game_id, sport)
        rng = _rng_for(game_id + "_final")

        if sport == Sport.MLB:
            home_score = max(0, int(rng.gauss(4.6, 2.6)))
            away_score = max(0, int(rng.gauss(4.3, 2.6)))
            if home_score == away_score:
                home_score += 1
            player_stats = {}
            for team in (ctx.home_team, ctx.away_team):
                for p in team.roster:
                    if p.is_pitcher:
                        if team.confirmed_starter and p.player_id == team.confirmed_starter.player_id:
                            player_stats[p.player_id] = {
                                "strikeouts": max(0, int(rng.gauss(p.k_per_9 * 0.65, 2))),
                            }
                    elif p.lineup_spot:
                        player_stats[p.player_id] = {
                            "hits": max(0, int(rng.gauss(p.obp * 4.3, 1.0))),
                            "rbis": max(0, int(rng.gauss(p.slg * 1.8, 1.0))),
                        }
        else:
            home_score = max(70, int(rng.gauss(ctx.home_team.team_ortg, 9)))
            away_score = max(70, int(rng.gauss(ctx.away_team.team_ortg, 9)))
            if home_score == away_score:
                home_score += rng.choice([-3, 3])
            player_stats = {}
            for team in (ctx.home_team, ctx.away_team):
                for p in team.roster:
                    player_stats[p.player_id] = {
                        "points": max(0, int(rng.gauss(p.ppg, p.ppg * 0.35 + 1))),
                        "assists": max(0, int(rng.gauss(p.apg, 1.5))),
                        "rebounds": max(0, int(rng.gauss(p.rpg, 2.0))),
                    }

        return {"home_score": home_score, "away_score": away_score,
                "player_stats": player_stats}
