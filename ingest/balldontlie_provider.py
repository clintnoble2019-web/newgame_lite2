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

TIMEZONE FIX (2026-07-08):
    BallDontLie timestamps are UTC. A 5:05 PM Pacific game is stamped
    ~00:05Z the NEXT calendar day, so storing the raw UTC date shifted
    every evening game forward one day — which is how a "tomorrow"
    prediction got generated for a game already in the 9th inning.
    All game dates are now converted to LOCAL_TZ (config.LOCAL_TZ,
    default America/Los_Angeles) before storage/comparison, and date
    queries fetch a two-day UTC window then filter by LOCAL date.

    Windows note: run `pip install tzdata` once (Windows has no system
    timezone database; Linux/Railway needs nothing).

Docs: https://docs.balldontlie.io/  (NBA)  ·  https://mlb.balldontlie.io/
Auth: header "Authorization: <api_key>" — no "Bearer" prefix.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

import config
from ingest.base import DataProvider
from models import (
    GameContext, PlayerStats, TeamData,
    Sport, GameStatus, InjuryStatus,
)

BASE = "https://api.balldontlie.io"

# Local timezone for all game-date logic. Override in config.py with
# e.g. LOCAL_TZ = "America/Chicago" — defaults to Pacific.
LOCAL_TZ = ZoneInfo(getattr(config, "LOCAL_TZ", "America/Los_Angeles"))

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


def _parse_local_dt(raw_date: str):
    """Parse a BallDontLie date field into a LOCAL_TZ-aware datetime.

    Handles both full ISO timestamps ('2026-07-09T00:05:00Z' — MLB) and
    bare dates ('2026-07-08'). Bare dates carry no time component, so
    they're taken at face value (assumed already the intended game date).
    Returns None if unparseable."""
    if not raw_date:
        return None
    try:
        if "T" in raw_date:
            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            if dt.tzinfo is None:          # naive timestamp -> assume UTC
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(LOCAL_TZ)
        # bare date — no rollover risk, pin to local midnight
        return datetime.fromisoformat(raw_date[:10]).replace(tzinfo=LOCAL_TZ)
    except ValueError:
        return None


def _fmt_time(dt: datetime) -> str:
    """Windows-safe 12-hour clock ('%-I' raises ValueError on Windows
    strftime — the old code silently swallowed that and left game_time
    blank on local runs). '%I' then strip the leading zero instead."""
    return dt.strftime("%I:%M %p").lstrip("0")


def _score(g: dict, side: str) -> int:
    """Score field names differ by BallDontLie sport. CONFIRMED from a
    real MLB response (2026-07-08): score lives under a nested
    '{side}_team_data' object as 'runs' — e.g. g['home_team_data']['runs'].
    NBA uses flat 'home_team_score'/'visitor_team_score' instead.
    Check the confirmed MLB shape first, then fall back through the
    other plausible variants for safety."""
    team_data = g.get(f"{side}_team_data")
    if isinstance(team_data, dict) and team_data.get("runs") is not None:
        return _i(team_data.get("runs"))

    candidates = [
        f"{side}_team_score", f"{side}_score",
        f"{side}_team_runs", f"{side}_runs",
    ]
    if side == "away":
        candidates += ["visitor_team_score", "visitor_score"]
    for key in candidates:
        val = g.get(key)
        if val is not None:
            return _i(val)
    score_obj = g.get("score", {})
    if isinstance(score_obj, dict):
        for key in (f"{side}_score", f"{side}_team_score",
                    "visitor_score" if side == "away" else None):
            if key and score_obj.get(key) is not None:
                return _i(score_obj.get(key))
    return 0


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
        """FREE tier endpoint — schedule + score shell only.

        date_str is a LOCAL calendar date ('2026-07-08'). BallDontLie's
        dates[] filter is keyed on the UTC timestamp, so an evening
        Pacific game lives under the NEXT UTC date. Query a two-day UTC
        window (requested date + next day), then filter client-side on
        each game's LOCAL date. Correct regardless of which convention
        BDL uses for dates[], since the filter is authoritative."""
        try:
            next_day = (datetime.fromisoformat(date_str[:10])
                        + timedelta(days=1)).strftime("%Y-%m-%d")
            query_dates = [date_str[:10], next_day]
        except ValueError:
            query_dates = [date_str]

        data = self._get(sport, "games", params={"dates[]": query_dates})

        seen, shells = set(), []
        for g in data.get("data", []):
            gid = str(g.get("id", ""))
            if gid in seen:
                continue
            seen.add(gid)
            shell = self._parse_game_shell(g, sport)
            if shell.game_date == date_str[:10]:
                shells.append(shell)
        return shells

    def get_game_context(self, game_id: str, sport: Sport) -> GameContext:
        """GOAT tier: lineups + injuries + season stats hydration.

        Lineup data is only posted once a game begins (BallDontLie
        data-availability limitation, not a bug) — for any game
        predicted ahead of first pitch, the live lineup comes back
        empty. Rather than fall straight to fully synthetic "Team
        Batter N" placeholders, first try the team's real active
        roster (real names) as a better-tier fallback. Individual
        stats still hydrate through the normal recent->career->team_avg
        chain — only the NAME identity improves here, not fabricated
        stats."""
        data = self._get(sport, f"games/{game_id}")
        shell = self._parse_game_shell(data.get("data", data), sport)

        lineup_data = self._get(sport, "lineups",
                                params={"game_ids[]": game_id})
        self._apply_lineup(shell, lineup_data.get("data", []), sport)

        # Live lineup wasn't posted -> use real roster names instead of
        # fully synthetic placeholders (MLB only for now).
        if sport == Sport.MLB:
            for team in (shell.home_team, shell.away_team):
                has_batters = any(not p.is_pitcher for p in team.roster)
                if not has_batters:
                    self._apply_active_roster_fallback(team, sport)

        self._hydrate_team_stats(shell.home_team, sport)
        self._hydrate_team_stats(shell.away_team, sport)
        for p in shell.home_team.roster + shell.away_team.roster:
            self._hydrate_player_stats(p, sport)

        return shell

    def _apply_active_roster_fallback(self, team: TeamData, sport: Sport):
        """Real-roster fallback tier for when the live lineup isn't
        posted yet. Pulls the team's active players (real names) and
        assigns the first 9 non-pitchers to lineup spots 1-9 —
        alphabetical/API order, not a real batting order, but real
        identities. Stats still hydrate normally afterward via
        _hydrate_player_stats (recent -> career -> team_avg chain)."""
        try:
            data = self._get(sport, "players/active",
                             params={"team_ids[]": team.team_id})
        except requests.RequestException:
            return   # falls through to the fully synthetic placeholder
                     # fallback already built into engine/mlb_sim.py

        players = [p for p in data.get("data", [])
                  if str(p.get("team", {}).get("id", "")) == team.team_id]
        if not players:
            return

        spot = 1
        for p in players:
            position = (p.get("position") or "").upper()
            if "PITCHER" in position or position in ("P", "SP", "RP"):
                continue   # bullpen/rotation avg handles pitchers separately
            if spot > 9:
                break
            pid = str(p.get("id", ""))
            name = (f"{p.get('first_name','')} "
                   f"{p.get('last_name','')}").strip() or pid
            team.roster.append(PlayerStats(
                player_id=pid, name=name, sport=sport, lineup_spot=spot))
            spot += 1

    def get_live_scores(self, sport: Sport) -> list[dict]:
        """FREE tier for schedule/score; GOAT unlocks true live box
        score granularity, but the base games endpoint updates scores
        in near-real-time already, which covers the ticker's needs.

        'Today' means the LOCAL calendar date — not the server's clock.
        On Railway the server runs UTC, so the old datetime.now() call
        rolled the ticker to tomorrow's slate every evening Pacific.
        Same two-day-window + local-date-filter approach as
        get_games_for_date."""
        today_local = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        next_day = (datetime.now(LOCAL_TZ)
                    + timedelta(days=1)).strftime("%Y-%m-%d")
        data = self._get(sport, "games",
                         params={"dates[]": [today_local, next_day]})

        out, seen = [], set()
        for g in data.get("data", []):
            gid = str(g.get("id", ""))
            if gid in seen:
                continue
            seen.add(gid)

            local_dt = _parse_local_dt(g.get("date", ""))
            game_local_date = (local_dt.strftime("%Y-%m-%d")
                               if local_dt else str(g.get("date", ""))[:10])
            if game_local_date != today_local:
                continue   # tomorrow's slate leaked in via the window

            home = g.get("home_team", {})
            away = g.get("away_team") or g.get("visitor_team", {})
            status = _game_status(g.get("status", ""))
            out.append({
                "game_id": gid,
                "status": status.value,
                "home": home.get("abbreviation", "HOM"),
                "away": away.get("abbreviation", "AWY"),
                "home_score": _score(g, "home"),
                "away_score": _score(g, "away"),
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

    def get_boxscore(self, game_id: str, sport: Sport) -> dict:
        """CONFIRMED against BallDontLie's published MLB OpenAPI spec
        (openapi/mlb.yml) on 2026-07-11: the game object itself carries
        home_team_data.inning_scores / away_team_data.inning_scores
        (array of per-inning runs) — no separate call needed. NBA
        carries the same idea as flat home_q1..q4/visitor_q1..q4 fields
        (there is no away_q* — BallDontLie uses 'visitor' for NBA
        scoring fields specifically, same legacy naming as visitor_team
        elsewhere in this file).

        Also works for LIVE games, not just FINAL — the stats endpoint
        returns whatever's accrued so far."""
        game = self._get(sport, f"games/{game_id}").get("data", {})
        stats = self._get(sport, "stats", params={"game_ids[]": game_id})
        box = self._parse_boxscore(game, stats.get("data", []), sport)

        line_score = []
        if sport == Sport.MLB:
            home_innings = (game.get("home_team_data") or {}).get("inning_scores") or []
            away_innings = (game.get("away_team_data") or {}).get("inning_scores") or []
            for i in range(max(len(home_innings), len(away_innings))):
                line_score.append({
                    "label": str(i + 1),
                    "home": home_innings[i] if i < len(home_innings) else 0,
                    "away": away_innings[i] if i < len(away_innings) else 0,
                })
        else:
            for i in range(1, 5):
                h, a = game.get(f"home_q{i}"), game.get(f"visitor_q{i}")
                if h is None and a is None:
                    continue
                line_score.append({"label": f"Q{i}", "home": h or 0, "away": a or 0})
            for i in range(1, 4):
                h, a = game.get(f"home_ot{i}"), game.get(f"visitor_ot{i}")
                if h is None and a is None:
                    continue
                line_score.append({"label": f"OT{i}", "home": h or 0, "away": a or 0})

        box["line_score"] = line_score
        inning_num = game.get("period")
        box["period"] = (f"Inning {inning_num}" if sport == Sport.MLB and inning_num
                         else (game.get("status_detail") or game.get("status")))
        return box

    # ── parsers ──────────────────────────────────────────────────────
    def _parse_game_shell(self, g: dict, sport: Sport) -> GameContext:
        home_j = g.get("home_team", {})
        # BallDontLie uses 'visitor_team' for NBA (legacy naming) but
        # MLB — added later — uses 'away_team' instead (confirmed).
        away_j = g.get("away_team") or g.get("visitor_team", {})

        home = TeamData(
            team_id=str(home_j.get("id", "")),
            # CONFIRMED: top-level 'home_team_name' is a reliable flat
            # string, present even when the nested object's naming
            # varies by sport. Prefer it, fall back to nested object.
            name=g.get("home_team_name") or home_j.get("display_name")
                 or home_j.get("full_name") or home_j.get("name", "Home"),
            abbrev=home_j.get("abbreviation", "HOM"), sport=sport)
        away = TeamData(
            team_id=str(away_j.get("id", "")),
            name=g.get("away_team_name") or away_j.get("display_name")
                 or away_j.get("full_name") or away_j.get("name", "Away"),
            abbrev=away_j.get("abbreviation", "AWY"), sport=sport)

        # TIMEZONE FIX: game_date/game_time are LOCAL now. The raw BDL
        # timestamp is UTC — a 5:05 PM Pacific start is ~00:05Z the next
        # day, so the old raw_date[:10] slice stamped every evening game
        # with tomorrow's date.
        raw_date = g.get("date", "")
        local_dt = _parse_local_dt(raw_date)
        if local_dt is not None:
            game_date = local_dt.strftime("%Y-%m-%d")
            game_time = _fmt_time(local_dt) if "T" in raw_date else ""
        else:
            game_date, game_time = raw_date[:10], ""

        return GameContext(
            game_id=str(g.get("id", "")), sport=sport,
            status=_game_status(g.get("status", "")),
            home_team=home, away_team=away,
            game_date=game_date, game_time=game_time,
            venue=g.get("venue", "") or "",
            home_score_live=_score(g, "home"),
            away_score_live=_score(g, "away"),
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
        team OBP/SLG for MLB; ORtg/DRtg/pace for NBA).

        CONFIRMED (diagnostic run against real API): 'season_stats' is
        actually the PLAYER-level endpoint — 'teams/season_stats' is
        the real team-level one, returning one row per team with both
        batting_obp and pitching_era populated. The team_ids[] filter
        doesn't appear to narrow results server-side (still returns
        all 30 teams), so match by team_id client-side instead."""
        if sport == Sport.MLB:
            try:
                data = self._get(sport, "teams/season_stats",
                                 params={"season": self.season})
            except requests.RequestException:
                return
            rows = data.get("data", [])
            if not rows:
                return
            s = None
            for row in rows:
                row_team = row.get("team", {})
                if str(row_team.get("id", "")) == team.team_id:
                    s = row
                    break
            if s is None:
                return   # this team not found in the response — keep defaults
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
        home_score = _score(game, "home")
        away_score = _score(game, "away")

        player_stats = {}
        for row in stat_rows:
            player = row.get("player", {})
            pid = str(player.get("id", ""))
            if not pid:
                continue
            name = (f"{player.get('first_name','')} "
                   f"{player.get('last_name','')}").strip()
            out = {}
            if sport == Sport.MLB:
                # CONFIRMED against BallDontLie's published MLB OpenAPI
                # spec (openapi/mlb.yml) on 2026-07-11: stats rows are
                # flat, not nested under 'batting'/'pitching' — every
                # row carries both sets of fields, null where they
                # don't apply. The old field names here (batting_h,
                # batting_rbi, pitching_k) don't exist in the real
                # response at all, so this was silently matching zero
                # players on every settle for this provider — a much
                # bigger bug than the modal that surfaced it.
                if row.get("hits") is not None:
                    out["hits"] = _i(row.get("hits"))
                    out["rbis"] = _i(row.get("rbi"))
                if row.get("ip") is not None:
                    out["strikeouts"] = _i(row.get("p_k"))
            else:
                out["points"] = _i(row.get("pts"))
                out["assists"] = _i(row.get("ast"))
                out["rebounds"] = _i(row.get("reb"))
            if out:
                out["_name"] = name   # underscore prefix: metadata, not a stat
                player_stats[pid] = out

        return {"home_score": home_score, "away_score": away_score,
                "player_stats": player_stats}
