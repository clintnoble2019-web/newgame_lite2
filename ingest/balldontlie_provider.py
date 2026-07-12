"""
NexGame Lite — BallDontLie Provider (PRODUCTION — GOAT tier, $79.98/mo)
Kage Software · 2026

PRIMARY production data source (replaces the MySportsFeeds path —
that provider is kept as a backup option in msf_provider.py).

GOAT tier ($39.99/mo per sport, both sports = $79.98/mo locked) unlocks:
    Lineups            -> confirmed starters (System 1 Branch B)
    Player Injuries     -> injury probability weights (LOCKED)
    Player Season Stats -> rolling averages, fallback chain (LOCKED)
    Team Season Stats   -> rotation/bullpen/team fallback constants
    Live Box Scores     -> live score ticker (System 4)
    Game Player Stats    -> settling pipeline input

ACTIVATION:
    1. Set BDL_API_KEY in config.py (or BDL_API_KEY env var on Railway)
    2. Set DATA_PROVIDER = "balldontlie"
    3. Run one real game through get_game_context() + get_final_boxscore(),
       print the raw JSON, diff against the parsers below. A few endpoint
       shapes here (season averages category/type pairing, exact lineup
       field for "confirmed starting pitcher" on MLB) are built from
       BallDontLie's public docs/examples, not a live test call — mark
       "VERIFY" comments below are the ones most likely to need a tweak.

Docs: https://docs.balldontlie.io/  (NBA)  ·  https://mlb.balldontlie.io/
Auth: header "Authorization: <api_key>" — no "Bearer" prefix.
"""

from datetime import datetime
import requests

import config
from ingest.base import DataProvider
from models import (
    GameContext, PlayerStats, TeamData,
    Sport, GameStatus, InjuryStatus,
)

BASE = "https://api.balldontlie.io"

_STATUS_MAP = {
    "scheduled": GameStatus.SCHEDULED,
    "final": GameStatus.FINAL,
}

_INJURY_MAP = {
    "out": InjuryStatus.OUT,
    "ir": InjuryStatus.IR,
    "injured reserve": InjuryStatus.IR,
    "questionable": InjuryStatus.QUESTIONABLE,
    "doubtful": InjuryStatus.QUESTIONABLE,
    "day-to-day": InjuryStatus.QUESTIONABLE,
    "probable": InjuryStatus.PROBABLE,
}


def _game_status(raw: str) -> GameStatus:
    """BallDontLie's MLB status field returns ESPN-style codes like
    'STATUS_FINAL', 'STATUS_SCHEDULED', 'STATUS_IN_PROGRESS'. NBA uses
    plain text ('Final', '2nd Qtr') or an ISO timestamp for games not
    yet started. Normalize all of these defensively."""
    r = (raw or "").strip().lower().replace("status_", "")
    if r in ("final", "closed", "completed"):
        return GameStatus.FINAL
    if r in ("", "scheduled", "pre", "preview"):
        return GameStatus.SCHEDULED
    if "t" in r and r[:4].isdigit():   # looks like an ISO timestamp
        return GameStatus.SCHEDULED
    if "postponed" in r or "cancel" in r:
        return GameStatus.POSTPONED
    return GameStatus.LIVE   # in_progress, 2nd_qtr, top_5th, etc.


def _injury_status(raw: str) -> InjuryStatus:
    return _INJURY_MAP.get((raw or "").strip().lower(), InjuryStatus.ACTIVE)


def _f(val, default=0.0):
    try:
        return float(val) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _i(val, default=0):
    try:
        return int(val) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


class BallDontLieProvider(DataProvider):
    """Production provider — GOAT tier, MLB + NBA. Same DataProvider
    interface as mock/free/mysportsfeeds — engine, settling, and
    dashboard code never change when this is the active provider."""

    def __init__(self):
        self.headers = {"Authorization": config.BDL_API_KEY}
        self.season = config.BDL_SEASON

    def _league(self, sport: Sport) -> str:
        return "nba" if sport == Sport.NBA else "mlb"

    def _get(self, sport: Sport, path: str, params: dict = None) -> dict:
        url = f"{BASE}/{self._league(sport)}/v1/{path}"
        r = requests.get(url, headers=self.headers, params=params or {},
                         timeout=20)
        r.raise_for_status()
        return r.json()

    # ── interface ────────────────────────────────────────────────────
    def get_games_for_date(self, sport: Sport, date_str: str) -> list[GameContext]:
        """FREE tier endpoint — schedule + score shell only."""
        data = self._get(sport, "games", params={"dates[]": date_str})
        return [self._parse_game_shell(g, sport) for g in data.get("data", [])]

    def get_game_context(self, game_id: str, sport: Sport) -> GameContext:
        """GOAT tier: lineups + injuries + season stats hydration."""
        data = self._get(sport, f"games/{game_id}")
        shell = self._parse_game_shell(data.get("data", data), sport)

        lineup_data = self._get(sport, "lineups",
                                params={"game_ids[]": game_id})
        self._apply_lineup(shell, lineup_data.get("data", []), sport)

        self._hydrate_team_stats(shell.home_team, sport)
        self._hydrate_team_stats(shell.away_team, sport)
        for p in shell.home_team.roster + shell.away_team.roster:
            self._hydrate_player_stats(p, sport)

        return shell

    def get_live_scores(self, sport: Sport) -> list[dict]:
        """FREE tier for schedule/score; GOAT unlocks true live box
        score granularity, but the base games endpoint updates scores
        in near-real-time already, which covers the ticker's needs."""
        date_compact = datetime.now().strftime("%Y-%m-%d")
        data = self._get(sport, "games", params={"dates[]": date_compact})
        out = []
        for g in data.get("data", []):
            home = g.get("home_team", {})
            away = g.get("away_team") or g.get("visitor_team", {})
            status = _game_status(g.get("status", ""))
            away_score = g.get("away_team_score")
            if away_score is None:
                away_score = g.get("visitor_team_score")
            out.append({
                "game_id": str(g.get("id", "")),
                "status": status.value,
                "home": home.get("abbreviation", "HOM"),
                "away": away.get("abbreviation", "AWY"),
                "home_score": _i(g.get("home_team_score")),
                "away_score": _i(away_score),
                "period": (g.get("status_detail") or g.get("status", "")
                          if status == GameStatus.LIVE
                          else status.value.upper()),
            })
        return out

    def get_final_boxscore(self, game_id: str, sport: Sport) -> dict:
        """GOAT tier — Game Player Stats -> settling pipeline input."""
        game = self._get(sport, f"games/{game_id}").get("data", {})
        stats = self._get(sport, "stats", params={"game_ids[]": game_id})
        return self._parse_boxscore(game, stats.get("data", []), sport)

    # ── parsers ──────────────────────────────────────────────────────
    def _parse_game_shell(self, g: dict, sport: Sport) -> GameContext:
        home_j = g.get("home_team", {})
        # BallDontLie uses 'visitor_team' for NBA (legacy naming) but
        # MLB — added later — may use 'away_team' instead. Check both.
        away_j = g.get("away_team") or g.get("visitor_team", {})

        home = TeamData(
            team_id=str(home_j.get("id", "")),
            name=home_j.get("display_name") or home_j.get("full_name")
                 or home_j.get("name", "Home"),
            abbrev=home_j.get("abbreviation", "HOM"), sport=sport)
        away = TeamData(
            team_id=str(away_j.get("id", "")),
            name=away_j.get("display_name") or away_j.get("full_name")
                 or away_j.get("name", "Away"),
            abbrev=away_j.get("abbreviation", "AWY"), sport=sport)

        raw_date = g.get("date", "")
        game_date, game_time = raw_date[:10], ""
        if "T" in raw_date:
            try:
                dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                game_date = dt.strftime("%Y-%m-%d")
                game_time = dt.strftime("%-I:%M %p")
            except ValueError:
                pass

        return GameContext(
            game_id=str(g.get("id", "")), sport=sport,
            status=_game_status(g.get("status", "")),
            home_team=home, away_team=away,
            game_date=game_date, game_time=game_time,
            venue=g.get("venue", "") or "",
            home_score_live=_i(g.get("home_team_score")),
            away_score_live=_i(g.get("away_team_score")
                              if g.get("away_team_score") is not None
                              else g.get("visitor_team_score")),
        )

    def _apply_lineup(self, context: GameContext, lineup_rows: list,
                      sport: Sport):
        """DATA AVAILABILITY NOTE (BallDontLie docs, verbatim): lineup
        data is only available once the game begins, and only for
        recent seasons. Games far in the future will return an empty
        lineup — that's expected, not a bug. The rotation-avg fallback
        (LOCKED) handles this exactly as designed."""
        home_spot = away_spot = 1
        for row in lineup_rows:
            player = row.get("player", {})
            if not player:
                continue
            pid = str(player.get("id", ""))
            name = (f"{player.get('first_name','')} "
                   f"{player.get('last_name','')}").strip() or pid
            is_starter = bool(row.get("starter", False))
            position = row.get("position", player.get("position", ""))
            team_ref = player.get("team", {})
            team_id = str(team_ref.get("id", ""))

            target = None
            if team_id == context.home_team.team_id:
                target = context.home_team
            elif team_id == context.away_team.team_id:
                target = context.away_team
            if target is None:
                continue   # couldn't match team — skip rather than misfile

            if sport == Sport.MLB:
                # VERIFY: BallDontLie's exact field for "this is the
                # starting pitcher" vs. a position player isn't nailed
                # down from docs alone — inferring from position=="P"
                # combined with starter==True. Confirm on first live pull.
                if position in ("P", "SP") and is_starter and \
                        target.confirmed_starter is None:
                    sp = PlayerStats(
                        player_id=pid, name=name, sport=sport,
                        is_pitcher=True, is_starter=True)
                    target.confirmed_starter = sp
                    target.roster.append(sp)
                    continue
                if position in ("P", "SP", "RP"):
                    continue   # bullpen arms come via season stats, not lineup
                spot = home_spot if target is context.home_team else away_spot
                b = PlayerStats(player_id=pid, name=name, sport=sport,
                                lineup_spot=spot)
                target.roster.append(b)
                if target is context.home_team:
                    home_spot += 1
                else:
                    away_spot += 1
            else:
                p = PlayerStats(player_id=pid, name=name, sport=sport,
                                is_starter_nba=is_starter)
                target.roster.append(p)

    def _hydrate_team_stats(self, team: TeamData, sport: Sport):
        """Team-level fallback constants (rotation avg, bullpen avg,
        team OBP/SLG for MLB; ORtg/DRtg/pace for NBA)."""
        if sport == Sport.MLB:
            try:
                data = self._get(sport, "season_stats",
                                 params={"season": self.season,
                                        "team_ids[]": team.team_id})
            except requests.RequestException:
                return
            rows = data.get("data", [])
            if not rows:
                return
            s = rows[0]
            team.rotation_avg_era = round(_f(s.get("pitching_era"),
                                             config.LEAGUE_AVG_ERA), 2)
            team.rotation_avg_whip = round(_f(s.get("pitching_whip"),
                                              config.LEAGUE_AVG_WHIP), 2)
            k9 = 0.0
            ip = _f(s.get("pitching_ip"))
            if ip > 0:
                k9 = round(_f(s.get("pitching_k")) / ip * 9, 2)
            team.rotation_avg_k9 = k9 or 8.5
            team.bullpen_era = team.rotation_avg_era   # refine post-M4
            team.bullpen_whip = team.rotation_avg_whip
            team.team_obp = round(_f(s.get("batting_obp"), 0.320), 3)
            team.team_slg = round(_f(s.get("batting_slg"), 0.410), 3)
        else:
            # VERIFY: NBA team-level ORtg/DRtg/pace via the advanced
            # stats endpoint, aggregated by team_ids[] + season. Confirm
            # exact path (season_averages vs stats/advanced) on first pull.
            try:
                data = self._get(sport, "stats/advanced",
                                 params={"seasons[]": self.season,
                                        "team_ids[]": team.team_id})
            except requests.RequestException:
                return
            rows = data.get("data", [])
            if not rows:
                return
            # average across returned rows (per-game advanced rows)
            ortg = sum(_f(r.get("offensive_rating")) for r in rows) / len(rows)
            drtg = sum(_f(r.get("defensive_rating")) for r in rows) / len(rows)
            pace = sum(_f(r.get("pace")) for r in rows) / len(rows)
            team.team_ortg = round(ortg or config.LEAGUE_AVG_ORTG, 1)
            team.team_drtg = round(drtg or config.LEAGUE_AVG_DRTG, 1)
            team.team_pace = round(pace or config.LEAGUE_AVG_PACE, 1)

    def _hydrate_player_stats(self, player: PlayerStats, sport: Sport):
        """Rolling averages with the LOCKED fallback chain and the
        LOCKED sufficiency rule (pitchers need 5 games, batters/NBA
        have no minimum)."""
        if sport == Sport.MLB:
            try:
                data = self._get(sport, "season_stats",
                                 params={"season": self.season,
                                        "player_ids[]": player.player_id})
            except requests.RequestException:
                player.data_source = "team_avg"
                return
            rows = data.get("data", [])
            if not rows:
                player.data_source = "team_avg"
                return
            s = rows[0]
            if player.is_pitcher:
                games = _i(s.get("pitching_gs") or s.get("batting_gp"))
                player.games_played = games
                if games >= config.MLB_PITCHER_MIN_GAMES:
                    player.era = round(_f(s.get("pitching_era"),
                                          config.LEAGUE_AVG_ERA), 2)
                    player.whip = round(_f(s.get("pitching_whip"),
                                           config.LEAGUE_AVG_WHIP), 2)
                    ip = _f(s.get("pitching_ip"))
                    if ip > 0:
                        player.k_per_9 = round(_f(s.get("pitching_k")) / ip * 9, 2)
                        player.bb_per_9 = round(_f(s.get("pitching_bb")) / ip * 9, 2)
                        player.hr_per_9 = round(_f(s.get("pitching_hr")) / ip * 9, 2)
                    player.data_source = "recent"
                else:
                    player.data_source = "career"  # caller applies rotation avg
            else:
                player.games_played = _i(s.get("batting_gp"))
                player.obp = round(_f(s.get("batting_obp"), 0.320), 3)
                player.slg = round(_f(s.get("batting_slg"), 0.410), 3)
                # BABIP not in the confirmed field list — approximate
                # from AVG until verified against a live pull.
                player.babip = round(_f(s.get("batting_avg"), 0.300), 3)
                player.data_source = ("recent" if player.games_played
                                      else "team_avg")
        else:
            # VERIFY: NBA season averages use a category+type pairing
            # per BallDontLie docs (e.g. general/base). Using the
            # simplest documented pairing here — confirm on first pull.
            try:
                data = self._get(sport, "season_averages/general",
                                 params={"season": self.season,
                                        "type": "base",
                                        "player_ids[]": player.player_id})
            except requests.RequestException:
                player.data_source = "team_avg"
                return
            rows = data.get("data", [])
            if not rows:
                player.data_source = "team_avg"
                return
            s = rows[0]
            player.games_played = _i(s.get("gp"))
            player.ppg = round(_f(s.get("pts")), 1)
            player.apg = round(_f(s.get("ast")), 1)
            player.rpg = round(_f(s.get("reb")), 1)
            player.true_shooting = round(_f(s.get("ts_pct"), 0.560), 3)
            player.minutes_proj = round(_f(s.get("min"), 18.0), 1)
            # usage_rate lives in the advanced endpoint, not general/base —
            # left at dataclass default (team_avg-equivalent) unless the
            # advanced pull above already set something at the team level.
            player.data_source = ("recent" if player.games_played
                                  else "team_avg")

    def _parse_boxscore(self, game: dict, stat_rows: list,
                        sport: Sport) -> dict:
        home_score = _i(game.get("home_team_score"))
        away_score = _i(game.get("away_team_score")
                        if game.get("away_team_score") is not None
                        else game.get("visitor_team_score"))

        # Team abbrevs so the frontend can split the box score by team
        home_j = game.get("home_team", {})
        away_j = game.get("away_team") or game.get("visitor_team", {})
        home_id = str(home_j.get("id", ""))
        away_id = str(away_j.get("id", ""))
        home_abbrev = home_j.get("abbreviation", "HOM")
        away_abbrev = away_j.get("abbreviation", "AWY")

        player_stats = {}
        for row in stat_rows:
            player = row.get("player", {})
            pid = str(player.get("id", ""))
            if not pid:
                continue
            # Tag each player with their team so the frontend/modal can
            # group by team instead of one mixed list. Stat rows carry a
            # team ref either on the row or nested on the player.
            team_ref = row.get("team") or player.get("team") or {}
            row_team_id = str(team_ref.get("id", ""))
            if row_team_id == home_id:
                team_tag = home_abbrev
            elif row_team_id == away_id:
                team_tag = away_abbrev
            else:
                team_tag = team_ref.get("abbreviation", "")
            out = {}
            if sport == Sport.MLB:
                if "batting_h" in row or "batting_rbi" in row:
                    out["hits"] = _i(row.get("batting_h"))
                    out["rbis"] = _i(row.get("batting_rbi"))
                if "pitching_k" in row:
                    out["strikeouts"] = _i(row.get("pitching_k"))
            else:
                out["points"] = _i(row.get("pts"))
                out["assists"] = _i(row.get("ast"))
                out["rebounds"] = _i(row.get("reb"))
            if out:
                out["_team"] = team_tag
                # settle/pipeline.py's name-fallback matching reads
                # actual_stats["_name"] — this provider never set it,
                # so that fallback silently never fired. Fixed.
                out["_name"] = (f"{player.get('first_name', '')} "
                                f"{player.get('last_name', '')}").strip()
                player_stats[pid] = out

        # STATUS FIELD (added 2026-07-12): the last-out guard in
        # /api/settle checks box["status"] — without this key every
        # settle 409s, and before the guard existed the missing status
        # let an in-progress 0-0 boxscore settle as if final (the
        # NYY@WSH incident). Normalized via _game_status, same as
        # everywhere else.
        return {"home_score": home_score, "away_score": away_score,
                "status": _game_status(game.get("status", "")).value,
                "player_stats": player_stats}
