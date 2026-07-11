"""
NexGame Lite — MySportsFeeds Provider (PRODUCTION — Aug 25 release)
Kage Software · 2026

Commercial license: base + CORE + STATS + DETAILS addons ($150/mo locked).
    CORE    -> schedules, scores, venues       (slate + live ticker)
    STATS   -> gamelogs, stat totals           (rolling averages)
    DETAILS -> boxscores, lineups, play-by-play (lineups + settling)

ACTIVATION AT RELEASE:
    1. Fill MSF_API_KEY + MSF_PASSWORD in config.py
    2. Set DATA_PROVIDER = "mysportsfeeds"
    3. Run against the 30-day trial and diff real responses against the
       field names below — MSF's docs sometimes drift by season/sport.
       Every dict.get() here has a safe fallback so a missing/renamed
       field degrades gracefully instead of crashing.

API docs: https://www.mysportsfeeds.com/data-feeds/api-docs/
Auth: HTTP Basic (api_key : your account password)
Field names below match the documented v2.1 schema
(schedule{awayTeam,homeTeam}, score{awayScoreTotal,...}) — verify against
the live trial before flipping the switch on release day.
"""

import base64
from datetime import datetime
import requests

import config
from ingest.base import DataProvider
from models import (
    GameContext, PlayerStats, TeamData,
    Sport, GameStatus, InjuryStatus,
)

MSF_BASE = "https://api.mysportsfeeds.com/v2.1/pull"

_STATUS_MAP = {
    "unplayed": GameStatus.SCHEDULED,
    "upcoming": GameStatus.SCHEDULED,
    "live": GameStatus.LIVE,
    "inprogress": GameStatus.LIVE,
    "completed": GameStatus.FINAL,
    "final": GameStatus.FINAL,
    "postponed": GameStatus.POSTPONED,
}

_INJURY_MAP = {
    "out": InjuryStatus.OUT,
    "ir": InjuryStatus.IR,
    "injured-reserve": InjuryStatus.IR,
    "questionable": InjuryStatus.QUESTIONABLE,
    "doubtful": InjuryStatus.QUESTIONABLE,
    "probable": InjuryStatus.PROBABLE,
    "day-to-day": InjuryStatus.QUESTIONABLE,
    "dtd": InjuryStatus.QUESTIONABLE,
}


def _injury_status(raw: str) -> InjuryStatus:
    return _INJURY_MAP.get((raw or "").strip().lower(), InjuryStatus.ACTIVE)


def _game_status(raw: str) -> GameStatus:
    return _STATUS_MAP.get((raw or "").strip().lower(), GameStatus.TBD)


def _f(val, default=0.0):
    """Safe float coercion — MSF sometimes returns null or '' for
    stats with no sample yet."""
    try:
        return float(val) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _i(val, default=0):
    try:
        return int(val) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


class MySportsFeedsProvider(DataProvider):
    """Production provider. Same DataProvider interface as mock/free —
    engine, settling, and dashboard code never change when this swaps in."""

    def __init__(self):
        creds = f"{config.MSF_API_KEY}:{config.MSF_PASSWORD}"
        token = base64.b64encode(creds.encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}
        self.season = config.MSF_SEASON

    def _get(self, sport: Sport, path: str, params: dict = None) -> dict:
        league = "mlb" if sport == Sport.MLB else "nba"
        url = f"{MSF_BASE}/{league}/{self.season}/{path}"
        r = requests.get(url, headers=self.headers, params=params or {},
                         timeout=20)
        r.raise_for_status()
        return r.json()

    # ── interface ────────────────────────────────────────────────────
    def get_games_for_date(self, sport: Sport, date_str: str) -> list[GameContext]:
        """CORE addon — date/{YYYYMMDD}/games.json"""
        date_compact = date_str.replace("-", "")
        data = self._get(sport, f"date/{date_compact}/games.json")
        games = []
        for g in data.get("games", []):
            # CORE gives us the shell immediately; full roster/lineup
            # detail is filled in lazily via get_game_context() so the
            # slate view stays fast (no N+1 lineup calls for a list).
            games.append(self._parse_game_shell(g, sport))
        return games

    def get_game_context(self, game_id: str, sport: Sport) -> GameContext:
        """DETAILS addon — lineup.json (full rosters + confirmed
        starters + injuries). STATS addon feeds the rolling averages
        via _hydrate_player_stats()."""
        data = self._get(sport, "games.json",
                         params={"game": game_id, "force": "true"})
        games = data.get("games", [])
        if not games:
            raise ValueError(f"MySportsFeeds returned no game for {game_id}")
        shell = self._parse_game_shell(games[0], sport)

        lineup_data = self._get(sport, f"games/{game_id}/lineup.json")
        self._apply_lineup(shell, lineup_data, sport)

        # STATS addon: rolling averages + fallback chain
        self._hydrate_team_stats(shell.home_team, sport)
        self._hydrate_team_stats(shell.away_team, sport)
        for p in shell.home_team.roster + shell.away_team.roster:
            self._hydrate_player_stats(p, sport)

        return shell

    def get_live_scores(self, sport: Sport) -> list[dict]:
        """CORE addon — scoreboard, polled every LIVE_POLL_SECONDS."""
        date_compact = datetime.now().strftime("%Y%m%d")
        data = self._get(sport, f"date/{date_compact}/games.json")
        out = []
        for g in data.get("games", []):
            sched = g.get("schedule", {})
            score = g.get("score", {})
            away = sched.get("awayTeam", {})
            home = sched.get("homeTeam", {})
            status = _game_status(sched.get("playedStatus", ""))
            period = (score.get("currentPeriod")
                      or ("FINAL" if status == GameStatus.FINAL else
                          status.value.upper()))
            out.append({
                "game_id": str(sched.get("id", "")),
                "status": status.value,
                "home": home.get("abbreviation", "HOM"),
                "away": away.get("abbreviation", "AWY"),
                "home_score": _i(score.get("homeScoreTotal")),
                "away_score": _i(score.get("awayScoreTotal")),
                "period": str(period),
            })
        return out

    def get_final_boxscore(self, game_id: str, sport: Sport) -> dict:
        """DETAILS addon — boxscore.json -> settling pipeline input."""
        data = self._get(sport, f"games/{game_id}/boxscore.json")
        return self._parse_boxscore(data, sport)

    def get_boxscore(self, game_id: str, sport: Sport) -> dict:
        """boxscore.json updates during LIVE games too, so this call
        works pre-final. No verified per-period breakdown field on this
        endpoint — line_score comes back empty rather than guessing at
        a field name that might silently return wrong data."""
        data = self._get(sport, f"games/{game_id}/boxscore.json")
        box = self._parse_boxscore(data, sport)
        box["line_score"] = []
        sched = data.get("game", {}).get("schedule", {})
        box["period"] = sched.get("playedStatus")
        return box

    # ── parsers ──────────────────────────────────────────────────────
    def _parse_game_shell(self, game_json: dict, sport: Sport) -> GameContext:
        """Schedule + score only — no roster detail yet (that's Phase 2
        of get_game_context, via DETAILS addon lineup.json)."""
        sched = game_json.get("schedule", {})
        away_j = sched.get("awayTeam", {})
        home_j = sched.get("homeTeam", {})
        venue = sched.get("venue", {}) or {}

        away = TeamData(
            team_id=str(away_j.get("id", "")),
            name=away_j.get("name", away_j.get("abbreviation", "Away")),
            abbrev=away_j.get("abbreviation", "AWY"), sport=sport)
        home = TeamData(
            team_id=str(home_j.get("id", "")),
            name=home_j.get("name", home_j.get("abbreviation", "Home")),
            abbrev=home_j.get("abbreviation", "HOM"), sport=sport)

        start_time = sched.get("startTime", "")
        game_date, game_time = "", ""
        if start_time:
            try:
                dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                game_date = dt.strftime("%Y-%m-%d")
                game_time = dt.strftime("%-I:%M %p")
            except ValueError:
                game_date = start_time[:10]

        return GameContext(
            game_id=str(sched.get("id", "")), sport=sport,
            status=_game_status(sched.get("playedStatus", "")),
            home_team=home, away_team=away,
            game_date=game_date, game_time=game_time,
            venue=venue.get("name", ""),
        )

    def _apply_lineup(self, context: GameContext, lineup_data: dict,
                      sport: Sport):
        """DETAILS addon: confirmed starters, batting order, injuries.
        Fallback chain (LOCKED): missing lineup -> caller's team-avg
        fallback constants stay at their dataclass defaults, so the
        engine still has a value to work with."""
        for side, team in (("away", context.away_team),
                           ("home", context.home_team)):
            side_data = (lineup_data.get(f"{side}Team")
                        or lineup_data.get("lineups", {}).get(side, {}))
            actual = (side_data.get("actual", {})
                     if isinstance(side_data, dict) else {})
            lineup_positions = actual.get("lineupPositions", [])

            spot = 1
            for entry in lineup_positions:
                player = entry.get("player", {})
                if not player:
                    continue
                pid = str(player.get("id", ""))
                name = (f"{player.get('firstName','')} "
                       f"{player.get('lastName','')}").strip() or pid
                position = (entry.get("position", "")
                           or player.get("primaryPosition", ""))
                cur_injury = player.get("currentInjury")
                raw_injury = (cur_injury.get("playingProbability", "")
                             if isinstance(cur_injury, dict) else "")
                injury = _injury_status(raw_injury)

                if sport == Sport.MLB:
                    if position == "P" and spot == 1:
                        # Starting pitcher entry
                        sp = PlayerStats(
                            player_id=pid, name=name, sport=sport,
                            is_pitcher=True, is_starter=True,
                            injury_status=injury)
                        team.confirmed_starter = sp
                        team.roster.append(sp)
                        continue
                    if position in ("P", "SP", "RP"):
                        continue  # bullpen arms come via STATS, not lineup
                    b = PlayerStats(
                        player_id=pid, name=name, sport=sport,
                        lineup_spot=spot, injury_status=injury)
                    team.roster.append(b)
                    spot += 1
                else:
                    is_starter = entry.get("position", "").upper() not in (
                        "BENCH", "")
                    p = PlayerStats(
                        player_id=pid, name=name, sport=sport,
                        is_starter_nba=is_starter, injury_status=injury)
                    team.roster.append(p)

    def _hydrate_team_stats(self, team: TeamData, sport: Sport):
        """STATS addon: team-level rolling averages used as fallback
        constants (rotation avg, bullpen avg, team ORtg/DRtg/pace)."""
        try:
            data = self._get(sport, "team_stats_totals.json",
                             params={"team": team.team_id})
        except requests.RequestException:
            return  # keep dataclass defaults — fallback chain still works
        rows = data.get("teamStatsTotals", [])
        if not rows:
            return
        stats = rows[0].get("stats", {})

        if sport == Sport.MLB:
            pitching = stats.get("pitching", {})
            hitting = stats.get("hitting", {})
            team.rotation_avg_era = round(
                _f(pitching.get("earnedRunAvg"), config.LEAGUE_AVG_ERA), 2)
            team.rotation_avg_whip = round(
                _f(pitching.get("walksAndHitsPerInningPitched"),
                   config.LEAGUE_AVG_WHIP), 2)
            team.bullpen_era = team.rotation_avg_era
            team.bullpen_whip = team.rotation_avg_whip
            team.team_obp = round(_f(hitting.get("onBasePct"), 0.320), 3)
            team.team_slg = round(_f(hitting.get("slugAvg"), 0.410), 3)
        else:
            offense = stats.get("offense", {})
            defense = stats.get("defense", {})
            misc = stats.get("miscellaneous", {})
            team.team_ortg = round(
                _f(offense.get("offRtg"), config.LEAGUE_AVG_ORTG), 1)
            team.team_drtg = round(
                _f(defense.get("defRtg"), config.LEAGUE_AVG_DRTG), 1)
            team.team_pace = round(
                _f(misc.get("pace"), config.LEAGUE_AVG_PACE), 1)

    def _hydrate_player_stats(self, player: PlayerStats, sport: Sport):
        """STATS addon: rolling averages with the LOCKED fallback chain
            recent -> career -> (team avg stays at caller's default)
        and the LOCKED sufficiency rule:
            MLB pitchers need 5 games, batters/NBA have no minimum."""
        try:
            data = self._get(sport, "seasonal_player_stats.json",
                             params={"player": player.player_id})
        except requests.RequestException:
            player.data_source = "team_avg"
            return
        rows = data.get("playerStatsTotals", [])
        if not rows:
            player.data_source = "team_avg"
            return
        stats = rows[0].get("stats", {})

        if sport == Sport.MLB and player.is_pitcher:
            pitching = stats.get("pitching", {})
            games = _i(pitching.get("gamesStarted"))
            player.games_played = games
            if games >= config.MLB_PITCHER_MIN_GAMES:
                player.era = round(_f(pitching.get("earnedRunAvg"),
                                      config.LEAGUE_AVG_ERA), 2)
                player.whip = round(
                    _f(pitching.get("walksAndHitsPerInningPitched"),
                       config.LEAGUE_AVG_WHIP), 2)
                player.k_per_9 = round(_f(pitching.get("strikeoutsPer9"),
                                          8.5), 2)
                player.bb_per_9 = round(_f(pitching.get("walksPer9"), 3.2), 2)
                player.hr_per_9 = round(_f(pitching.get("homeRunsPer9"),
                                           1.1), 2)
                player.data_source = "recent"
            else:
                player.data_source = "career"  # caller applies rotation avg
        elif sport == Sport.MLB:
            hitting = stats.get("hitting", {})
            player.games_played = _i(hitting.get("gamesPlayed"))
            player.obp = round(_f(hitting.get("onBasePct"), 0.320), 3)
            player.slg = round(_f(hitting.get("slugAvg"), 0.410), 3)
            player.babip = round(
                _f(hitting.get("battingAvgOnBallsInPlay"), 0.300), 3)
            player.data_source = ("recent" if player.games_played
                                  else "team_avg")
        else:
            offense = stats.get("offense", {})
            misc = stats.get("miscellaneous", {})
            player.games_played = _i(offense.get("gamesPlayed"))
            player.ppg = round(_f(offense.get("ptsPerGame")), 1)
            player.apg = round(_f(offense.get("astPerGame")), 1)
            player.rpg = round(_f(offense.get("rebPerGame")), 1)
            player.usage_rate = round(_f(misc.get("usagePct"), 0.18), 3)
            player.true_shooting = round(_f(misc.get("trueShootingPct"),
                                            0.560), 3)
            player.minutes_proj = round(_f(offense.get("minPerGame"),
                                           18.0), 1)
            player.data_source = ("recent" if player.games_played
                                  else "team_avg")

    def _parse_boxscore(self, data: dict, sport: Sport) -> dict:
        """DETAILS addon boxscore -> settling pipeline input.
        Returns {'home_score', 'away_score', 'player_stats': {pid: {...}}}"""
        score = data.get("scoring", {}) or data.get("game", {}).get("score", {})
        home_score = _i(score.get("homeScoreTotal"))
        away_score = _i(score.get("awayScoreTotal"))

        player_stats = {}
        for side_key in ("awayTeam", "homeTeam"):
            side = data.get(side_key, {})
            for entry in side.get("players", []):
                player = entry.get("player", {})
                pid = str(player.get("id", ""))
                if not pid:
                    continue
                name = (f"{player.get('firstName','')} "
                       f"{player.get('lastName','')}").strip() or pid
                stats = entry.get("playerStats", entry.get("stats", {}))
                out = {}
                if sport == Sport.MLB:
                    batting = stats.get("batting", {})
                    pitching = stats.get("pitching", {})
                    if batting:
                        out["hits"] = _i(batting.get("hits"))
                        out["rbis"] = _i(batting.get("runsBattedIn"))
                    if pitching:
                        out["strikeouts"] = _i(
                            pitching.get("pitcherStrikeouts")
                            or pitching.get("strikeouts"))
                else:
                    offense = stats.get("offense", {})
                    out["points"] = _i(offense.get("pts"))
                    out["assists"] = _i(offense.get("ast"))
                    out["rebounds"] = _i(offense.get("reb")
                                         or offense.get("rebTotal"))
                if out:
                    out["_name"] = name
                    player_stats[pid] = out

        return {"home_score": home_score, "away_score": away_score,
                "player_stats": player_stats}
