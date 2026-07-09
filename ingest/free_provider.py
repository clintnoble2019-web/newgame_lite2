"""
NexGame Lite — Free API Provider (DEVELOPMENT ONLY)
Kage Software · 2026

MLB:  MLB Stats API (statsapi.mlb.com) — free, no key
NBA:  BallDontLie (api.balldontlie.io) — free tier

⚠️  NOT FOR PUBLIC RELEASE. These are dev/testing feeds only.
    Commercial release (Aug 25) uses MySportsFeedsProvider —
    swap DATA_PROVIDER in config.py.

Notes for Clint:
- This is the layer where API-specific parsing lives. Everything here
  translates raw JSON into our PlayerStats/TeamData/GameContext models.
- The fallback chain (recent -> career -> team avg) is applied here so
  the engine NEVER receives a null.
- If any endpoint shape changed since writing, fix the parse in the
  matching _parse_* helper — nothing outside this file cares.
"""

import requests
from datetime import datetime
import config
from ingest.base import DataProvider
from models import (
    GameContext, PlayerStats, TeamData,
    Sport, GameStatus, InjuryStatus,
)

MLB_BASE = "https://statsapi.mlb.com/api/v1"
BDL_BASE = "https://api.balldontlie.io/v1"
BDL_HEADERS = {"Authorization": ""}   # add free-tier key if you register one


def _mlb_status(abstract: str) -> GameStatus:
    return {
        "Preview": GameStatus.SCHEDULED,
        "Live": GameStatus.LIVE,
        "Final": GameStatus.FINAL,
    }.get(abstract, GameStatus.TBD)


def _injury_from_mlb(status_code: str) -> InjuryStatus:
    if status_code in ("D10", "D15", "D60"):
        return InjuryStatus.IR
    if status_code == "DTD":
        return InjuryStatus.QUESTIONABLE
    return InjuryStatus.ACTIVE


class FreeProvider(DataProvider):
    """Free dev APIs. Same interface, swap-ready."""

    # ── slate ────────────────────────────────────────────────────────
    def get_games_for_date(self, sport: Sport, date_str: str) -> list[GameContext]:
        if sport == Sport.MLB:
            return self._mlb_games(date_str)
        return self._nba_games(date_str)

    def _mlb_games(self, date_str: str) -> list[GameContext]:
        r = requests.get(f"{MLB_BASE}/schedule",
                         params={"sportId": 1, "date": date_str}, timeout=15)
        r.raise_for_status()
        games = []
        for day in r.json().get("dates", []):
            for g in day.get("games", []):
                games.append(self.get_game_context(str(g["gamePk"]), Sport.MLB))
        return games

    def _nba_games(self, date_str: str) -> list[GameContext]:
        r = requests.get(f"{BDL_BASE}/games",
                         params={"dates[]": date_str},
                         headers=BDL_HEADERS, timeout=15)
        r.raise_for_status()
        return [self.get_game_context(str(g["id"]), Sport.NBA)
                for g in r.json().get("data", [])]

    # ── full context ─────────────────────────────────────────────────
    def get_game_context(self, game_id: str, sport: Sport) -> GameContext:
        if sport == Sport.MLB:
            return self._mlb_context(game_id)
        return self._nba_context(game_id)

    def _mlb_context(self, game_pk: str) -> GameContext:
        r = requests.get(f"{MLB_BASE}.1/game/{game_pk}/feed/live", timeout=20)
        r.raise_for_status()
        data = r.json()
        game_data = data["gameData"]

        home = self._mlb_team(game_data["teams"]["home"],
                              data, side="home")
        away = self._mlb_team(game_data["teams"]["away"],
                              data, side="away")

        dt = game_data["datetime"]
        return GameContext(
            game_id=game_pk, sport=Sport.MLB,
            status=_mlb_status(game_data["status"]["abstractGameState"]),
            home_team=home, away_team=away,
            game_date=dt.get("officialDate", ""),
            game_time=dt.get("time", "") + dt.get("ampm", ""),
            venue=game_data.get("venue", {}).get("name", ""),
        )

    def _mlb_team(self, team_json: dict, live_feed: dict, side: str) -> TeamData:
        team = TeamData(
            team_id=str(team_json["id"]), name=team_json["name"],
            abbrev=team_json.get("abbreviation", team_json["name"][:3].upper()),
            sport=Sport.MLB,
        )
        # Probable pitcher (confirmed starter node)
        probable = (live_feed["gameData"]
                    .get("probablePitchers", {}).get(side))
        if probable:
            team.confirmed_starter = self._mlb_pitcher_stats(
                str(probable["id"]), probable["fullName"])
        # Rotation + team fallbacks from season team stats
        self._mlb_team_fallbacks(team)
        # Batting lineup from boxscore (if posted)
        self._mlb_lineup(team, live_feed, side)
        return team

    def _mlb_pitcher_stats(self, player_id: str, name: str) -> PlayerStats:
        """Pull pitcher season stats. Enforces 5-game minimum (LOCKED):
        under 5 starts -> career stats -> rotation avg handled upstream."""
        p = PlayerStats(player_id=player_id, name=name, sport=Sport.MLB,
                        is_pitcher=True, is_starter=True)
        r = requests.get(
            f"{MLB_BASE}/people/{player_id}/stats",
            params={"stats": "season", "group": "pitching"}, timeout=15)
        splits = (r.json().get("stats", [{}])[0].get("splits", [])
                  if r.ok else [])
        if splits:
            s = splits[0]["stat"]
            p.games_played = int(s.get("gamesStarted", 0))
            if p.games_played >= config.MLB_PITCHER_MIN_GAMES:
                p.era = round(float(s.get("era", 0) or 0), 2)
                p.whip = round(float(s.get("whip", 0) or 0), 2)
                p.k_per_9 = round(float(s.get("strikeoutsPer9Inn", 0) or 0), 2)
                p.bb_per_9 = round(float(s.get("walksPer9Inn", 0) or 0), 2)
                p.hr_per_9 = round(float(s.get("homeRunsPer9", 0) or 0), 2)
                p.data_source = "recent"
                return p
        # Career fallback
        r = requests.get(
            f"{MLB_BASE}/people/{player_id}/stats",
            params={"stats": "career", "group": "pitching"}, timeout=15)
        splits = (r.json().get("stats", [{}])[0].get("splits", [])
                  if r.ok else [])
        if splits:
            s = splits[0]["stat"]
            p.era = round(float(s.get("era", 0) or 0), 2)
            p.whip = round(float(s.get("whip", 0) or 0), 2)
            p.k_per_9 = round(float(s.get("strikeoutsPer9Inn", 0) or 0), 2)
            p.data_source = "career"
        return p

    def _mlb_team_fallbacks(self, team: TeamData):
        """Team-level pitching/hitting used by the fallback chain."""
        r = requests.get(
            f"{MLB_BASE}/teams/{team.team_id}/stats",
            params={"stats": "season", "group": "pitching,hitting"},
            timeout=15)
        if not r.ok:
            return
        for block in r.json().get("stats", []):
            group = block.get("group", {}).get("displayName", "")
            splits = block.get("splits", [])
            if not splits:
                continue
            s = splits[0]["stat"]
            if group == "pitching":
                team.rotation_avg_era = round(float(s.get("era", 4.20) or 4.20), 2)
                team.rotation_avg_whip = round(float(s.get("whip", 1.30) or 1.30), 2)
                team.rotation_avg_k9 = round(float(s.get("strikeoutsPer9Inn", 8.5) or 8.5), 2)
                team.bullpen_era = team.rotation_avg_era      # refine post-M4
                team.bullpen_whip = team.rotation_avg_whip
            elif group == "hitting":
                team.team_obp = round(float(s.get("obp", 0.320) or 0.320), 3)
                team.team_slg = round(float(s.get("slg", 0.410) or 0.410), 3)

    def _mlb_lineup(self, team: TeamData, live_feed: dict, side: str):
        """Posted lineup -> batter stats with fallback chain."""
        box = (live_feed.get("liveData", {})
               .get("boxscore", {}).get("teams", {}).get(side, {}))
        order = box.get("battingOrder", [])
        for spot, pid in enumerate(order[:9], start=1):
            b = self._mlb_batter_stats(str(pid), team)
            b.lineup_spot = spot
            team.roster.append(b)
        # No lineup posted -> synthetic team-average batters (fallback)
        if not order:
            for spot in range(1, 10):
                team.roster.append(PlayerStats(
                    player_id=f"{team.team_id}_avg{spot}",
                    name=f"{team.abbrev} Batter {spot}",
                    sport=Sport.MLB, lineup_spot=spot,
                    obp=team.team_obp or 0.320,
                    slg=team.team_slg or 0.410,
                    data_source="team_avg",
                ))

    def _mlb_batter_stats(self, player_id: str, team: TeamData) -> PlayerStats:
        """No minimum for batters (LOCKED) — any season data wins."""
        b = PlayerStats(player_id=player_id, name="", sport=Sport.MLB)
        r = requests.get(f"{MLB_BASE}/people/{player_id}", timeout=15)
        if r.ok:
            people = r.json().get("people", [])
            if people:
                b.name = people[0].get("fullName", player_id)
        r = requests.get(
            f"{MLB_BASE}/people/{player_id}/stats",
            params={"stats": "season", "group": "hitting"}, timeout=15)
        splits = (r.json().get("stats", [{}])[0].get("splits", [])
                  if r.ok else [])
        if splits:
            s = splits[0]["stat"]
            b.games_played = int(s.get("gamesPlayed", 0))
            b.obp = round(float(s.get("obp", 0) or 0), 3)
            b.slg = round(float(s.get("slg", 0) or 0), 3)
            b.babip = round(float(s.get("babip", 0) or 0), 3)
            b.data_source = "recent"
        else:
            b.obp = team.team_obp or 0.320
            b.slg = team.team_slg or 0.410
            b.data_source = "team_avg"
        return b

    def _nba_context(self, game_id: str) -> GameContext:
        """BallDontLie free tier: teams + season averages.
        Free tier lacks lineups/injuries — team-average fallbacks used.
        MySportsFeeds provides full lineups + injuries at release."""
        r = requests.get(f"{BDL_BASE}/games/{game_id}",
                         headers=BDL_HEADERS, timeout=15)
        r.raise_for_status()
        g = r.json()["data"]

        home = self._nba_team(g["home_team"])
        away = self._nba_team(g["visitor_team"])
        status = GameStatus.FINAL if g.get("status") == "Final" \
            else GameStatus.SCHEDULED

        return GameContext(
            game_id=game_id, sport=Sport.NBA, status=status,
            home_team=home, away_team=away,
            game_date=g.get("date", "")[:10], game_time="",
            home_score_live=g.get("home_team_score", 0),
            away_score_live=g.get("visitor_team_score", 0),
        )

    def _nba_team(self, team_json: dict) -> TeamData:
        team = TeamData(
            team_id=str(team_json["id"]), name=team_json["full_name"],
            abbrev=team_json["abbreviation"], sport=Sport.NBA,
            team_ortg=config.LEAGUE_AVG_ORTG,     # free tier: league avg
            team_drtg=config.LEAGUE_AVG_DRTG,     # MSF gives real values
            team_pace=config.LEAGUE_AVG_PACE,
        )
        # Synthetic rotation at team-average level (fallback chain endpoint)
        for i in range(10):
            starter = i < 5
            team.roster.append(PlayerStats(
                player_id=f"{team.abbrev}_p{i}",
                name=f"{team.abbrev} Player {i+1}", sport=Sport.NBA,
                ppg=14.0 if starter else 7.0,
                apg=3.5 if starter else 1.5,
                rpg=5.0 if starter else 2.5,
                usage_rate=0.24 if starter else 0.16,
                true_shooting=0.575,
                minutes_proj=32 if starter else 16,
                is_starter_nba=starter,
                data_source="team_avg",
            ))
        return team

    # ── live + settle ────────────────────────────────────────────────
    def get_live_scores(self, sport: Sport) -> list[dict]:
        date_str = datetime.now().strftime("%Y-%m-%d")
        out = []
        for g in self.get_games_for_date(sport, date_str):
            out.append({
                "game_id": g.game_id, "status": g.status.value,
                "home": g.home_team.abbrev, "away": g.away_team.abbrev,
                "home_score": g.home_score_live,
                "away_score": g.away_score_live,
                "period": g.period_live or g.status.value.upper(),
            })
        return out

    def get_final_boxscore(self, game_id: str, sport: Sport) -> dict:
        if sport == Sport.MLB:
            r = requests.get(f"{MLB_BASE}.1/game/{game_id}/feed/live",
                             timeout=20)
            r.raise_for_status()
            live = r.json()["liveData"]
            lines = live["linescore"]["teams"]
            player_stats = {}
            for side in ("home", "away"):
                box = live["boxscore"]["teams"][side]["players"]
                for pid_key, pdata in box.items():
                    pid = pid_key.replace("ID", "")
                    bat = pdata.get("stats", {}).get("batting", {})
                    pit = pdata.get("stats", {}).get("pitching", {})
                    stats = {}
                    if bat:
                        stats["hits"] = bat.get("hits", 0)
                        stats["rbis"] = bat.get("rbi", 0)
                    if pit:
                        stats["strikeouts"] = pit.get("strikeOuts", 0)
                    if stats:
                        player_stats[pid] = stats
            return {"home_score": lines["home"].get("runs", 0),
                    "away_score": lines["away"].get("runs", 0),
                    "player_stats": player_stats}

        # NBA — free tier gives final team scores only
        r = requests.get(f"{BDL_BASE}/games/{game_id}",
                         headers=BDL_HEADERS, timeout=15)
        r.raise_for_status()
        g = r.json()["data"]
        return {"home_score": g.get("home_team_score", 0),
                "away_score": g.get("visitor_team_score", 0),
                "player_stats": {}}
