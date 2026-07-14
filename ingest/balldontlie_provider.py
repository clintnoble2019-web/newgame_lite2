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

SETTLE STATUS FIX (2026-07-12):
    _parse_boxscore now includes "status" (normalized via _game_status)
    in its return — the last-out guard in /api/settle checks
    box["status"] before settling. Without this key every settle 409s;
    before the guard existed, the missing status let an in-progress
    0-0 boxscore settle as if final (the NYY@WSH incident). Player
    rows also carry "_team" (abbrev) so the frontend can group the
    box score by team.

PROBABLE STARTERS (2026-07-12):
    BDL lineups are empty until first pitch, so pre-game predictions
    always ran on rotation avg. get_game_context now pulls probable
    pitchers from MLB's free StatsAPI (verified live, no key needed)
    and sets confirmed_starter when the pitcher also resolves to a
    BDL player id (so real stats hydrate). Live lineup > probable >
    rotation avg, in that order. See _apply_probable_starters.

KALSHI SCHEDULE + ODDS (2026-07-14):
    NexGame Lite is now a gambling-analytics tool, not pure data
    science — Kalshi (CFTC-regulated, read-only public market data,
    no auth needed, VERIFIED live) is now the PRIMARY schedule filter
    across all four sports: get_games_for_date only returns games that
    exist as real Kalshi markets, matched by team NAME (not code —
    Kalshi's team codes are inconsistent across sports and CS2's are
    fully arbitrary per-event strings with no relationship to the
    team name at all) against BDL's own game data for the same date.

    Matching is done SHELL-BY-SHELL, never globally — for each Kalshi
    game, both its sides must match ONE SPECIFIC BDL game's home+away
    (in either order), so two different games' teams can never get
    cross-wired. A Kalshi game with no BDL match is skipped entirely
    (never shown with fabricated stats); a BDL game with no Kalshi
    market is also skipped (Kalshi genuinely drives what's shown now).
    See ingest/kalshi_client.py for the tested ticker-parsing and
    name-matching logic this depends on.

    Every existing per-game method (get_game_context, get_final_boxscore,
    settling) is UNCHANGED — they still receive a real BDL game_id and
    work exactly as before. Kalshi only changes WHICH games appear in
    the list, and attaches kalshi_event_ticker/kalshi_home_prob/
    kalshi_away_prob onto the GameContext for display.

Docs: https://docs.balldontlie.io/  (NBA)  ·  https://mlb.balldontlie.io/
Auth: header "Authorization: <api_key>" — no "Bearer" prefix.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

import config
from ingest.base import DataProvider
from ingest.kalshi_client import get_kalshi_games, match_team_by_name
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
    yet started. WNBA uses ESPN abstract states: 'pre' / 'in' / 'post'
    (VERIFIED live 2026-07-13 — every completed WNBA game is 'post',
    never 'final'; without this mapping completed games classified as
    LIVE and the settle guard blocked WNBA settling entirely). CS2
    uses its own vocabulary (VERIFIED live 2026-07-13): 'upcoming' /
    'finished' / 'defwin' (forfeit — has a real, settleable score) /
    'canceled'. Normalize all of these defensively."""
    r = (raw or "").strip().lower().replace("status_", "")
    if r in ("final", "closed", "completed", "post", "finished", "defwin"):
        return GameStatus.FINAL
    if r in ("", "scheduled", "pre", "preview", "upcoming"):
        return GameStatus.SCHEDULED
    if "t" in r and r[:4].isdigit():   # looks like an ISO timestamp
        return GameStatus.SCHEDULED
    if "postponed" in r or "cancel" in r:
        return GameStatus.POSTPONED
    return GameStatus.LIVE   # in_progress, in, 2nd_qtr, top_5th, etc.


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


def _match_kalshi_to_shell(kalshi_game: dict, shells: list):
    """Match one Kalshi game's two sides against a list of BDL
    GameContext shells for the same date. SHELL-BY-SHELL matching
    (never a global team-name lookup across all shells) so two
    different games' teams can never cross-wire — e.g. if two
    different Yankees games somehow existed for the same date, a
    global match could attach the wrong odds to the wrong game.

    Returns (shell, home_side, away_side) — home_side/away_side are
    the Kalshi side dicts correctly assigned to match the shell's
    actual home/away (Kalshi's own side order isn't guaranteed to
    match BDL's home/away convention). Returns (None, None, None) if
    no shell's both teams match this Kalshi game's both sides.

    TWO matching strategies per side, tried in order (VERIFIED live
    2026-07-14): name-based fuzzy match first (handles the normal
    case, e.g. Kalshi's truncated "New York M" vs BDL's "New York
    Mets"); falls back to exact code/abbreviation equality when name
    matching fails — needed for Toronto Tempo specifically, where BDL
    returned just "Tempo" (no city) so Kalshi's side name "Toronto"
    had nothing to substring-match against, even though both sides
    agree on the short code "TOR". New expansion teams are the most
    likely case to hit this gap; the abbreviation fallback is safe
    for every sport since abbreviations are unique per team."""
    sides = kalshi_game.get("sides", [])
    if len(sides) != 2:
        return None, None, None
    side_a, side_b = sides

    def side_matches_team(side: dict, team) -> bool:
        if match_team_by_name(side.get("name", ""), [{"name": team.name}]):
            return True
        code = (side.get("code") or "").strip().upper()
        abbrev = (getattr(team, "abbrev", "") or "").strip().upper()
        return bool(code) and code == abbrev

    for shell in shells:
        if (side_matches_team(side_a, shell.home_team)
                and side_matches_team(side_b, shell.away_team)):
            return shell, side_a, side_b
        if (side_matches_team(side_b, shell.home_team)
                and side_matches_team(side_a, shell.away_team)):
            return shell, side_b, side_a
    return None, None, None


def _mid_prob(side: dict) -> float:
    """Kalshi yes_bid/yes_ask midpoint as a 0-100 probability, matching
    home_win_pct's scale. Returns 0.0 if bid/ask are both zero (no
    liquidity yet) rather than a misleading 50.0."""
    bid, ask = side.get("yes_bid", 0.0), side.get("yes_ask", 0.0)
    if bid <= 0 and ask <= 0:
        return 0.0
    return round((bid + ask) / 2 * 100, 1)


class BallDontLieProvider(DataProvider):
    """Production provider — GOAT tier, MLB + NBA. Same DataProvider
    interface as mock/free/mysportsfeeds — engine, settling, and
    dashboard code never change when this is the active provider."""

    def __init__(self):
        self.headers = {"Authorization": config.BDL_API_KEY}
        self.season = config.BDL_SEASON

    def _league(self, sport: Sport) -> str:
        # WNBA and CS2 mirror the basketball/base endpoint conventions
        # at their own /v1/ roots — see per-sport branches below for
        # where the data SHAPES genuinely diverge (CS2 especially:
        # matches/team1/team2, not games/home/away).
        if sport == Sport.NBA:
            return "nba"
        if sport == Sport.WNBA:
            return "wnba"
        if sport == Sport.CS2:
            return "cs"
        return "mlb"

    def _get(self, sport: Sport, path: str, params: dict = None) -> dict:
        url = f"{BASE}/{self._league(sport)}/v1/{path}"
        r = requests.get(url, headers=self.headers, params=params or {},
                         timeout=20)
        r.raise_for_status()
        return r.json()

    # ── interface ────────────────────────────────────────────────────
    def get_games_for_date(self, sport: Sport, date_str: str) -> list[GameContext]:
        """PUBLIC ENTRY POINT — now Kalshi-gated (2026-07-14). Only
        games that exist as real Kalshi markets are returned, each
        enriched with kalshi_event_ticker/kalshi_home_prob/
        kalshi_away_prob. Falls back to the plain BDL list (no Kalshi
        fields) if the Kalshi fetch itself fails — a Kalshi outage
        shouldn't take the whole schedule down."""
        bdl_shells = self._bdl_shells_for_date(sport, date_str)

        try:
            kalshi_games = get_kalshi_games(sport.value, date_str)
        except Exception:
            return bdl_shells   # Kalshi unreachable — degrade gracefully

        if not kalshi_games:
            return []   # Kalshi is the schedule gate now — no markets,
                        # no games shown, even if BDL has some listed

        out = []
        for kg in kalshi_games:
            shell, home_side, away_side = _match_kalshi_to_shell(
                kg, bdl_shells)
            if shell is None:
                continue   # no confident BDL match — skip, never guess
            shell.kalshi_event_ticker = kg["event_ticker"]
            shell.kalshi_home_prob = _mid_prob(home_side)
            shell.kalshi_away_prob = _mid_prob(away_side)
            out.append(shell)
        return out

    def _bdl_shells_for_date(self, sport: Sport,
                             date_str: str) -> list[GameContext]:
        """The ORIGINAL get_games_for_date logic, renamed to a private
        helper — Kalshi-matching now sits on top of this, but every
        sport's BDL fetch behavior is completely unchanged."""
        if sport == Sport.CS2:
            return self._cs2_matches_for_date(date_str)

        """date_str is a LOCAL calendar date ('2026-07-08'). BallDontLie's
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

        try:
            data = self._get(sport, "games", params={"dates[]": query_dates})
        except requests.RequestException:
            return []

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

    def _cs2_matches_for_date(self, date_str: str) -> list[GameContext]:
        """CS2 SCHEDULE — DIFFERENT PATTERN FROM EVERY OTHER SPORT.

        VERIFIED live 2026-07-13: the generic /cs/v1/matches list
        (no filters) ALWAYS returns the same stale 2021 dataset —
        status/sort/date-range params are silently ignored. The ONLY
        confirmed way to reach real current matches is filtering by
        tournament_ids[] for tournaments whose status is literally
        "current" (date-range-overlap alone isn't enough — a
        cancelled tournament can still overlap today's date and will
        correctly return zero matches).

        So: pull tournaments, keep only status=="current" ones, pull
        each one's matches, keep matches whose start_time falls on
        date_str. Slower than a single query (N+1 calls) but it's the
        only verified-working path — CS2's tournament count active at
        once is small (single digits), so this stays cheap in practice.
        """
        try:
            t_data = self._get(Sport.CS2, "tournaments",
                               params={"per_page": 100})
        except requests.RequestException:
            return []
        current_ids = [t.get("id") for t in t_data.get("data", [])
                       if t.get("status") == "current"]

        shells = []
        for tid in current_ids:
            try:
                m_data = self._get(Sport.CS2, "matches",
                                   params={"tournament_ids[]": tid,
                                          "per_page": 100})
            except requests.RequestException:
                continue
            for m in m_data.get("data", []):
                shell = self._parse_cs2_match_shell(m)
                if shell and shell.game_date == date_str[:10]:
                    shells.append(shell)
        return shells

    def _parse_cs2_match_shell(self, m: dict) -> "GameContext | None":
        """Build a GameContext shell from one CS2 match record.
        team1/team2 can be None on not-yet-assigned bracket slots
        (VERIFIED live) — skip those, nothing to predict yet."""
        t1, t2 = m.get("team1"), m.get("team2")
        if not t1 or not t2:
            return None
        local_dt = _parse_local_dt(m.get("start_time", ""))
        if local_dt is None:
            return None
        home = TeamData(team_id=str(t1.get("id", "")),
                        name=t1.get("name", "Team 1"),
                        abbrev=(t1.get("short_name") or t1.get("name", ""))[:4],
                        sport=Sport.CS2)
        away = TeamData(team_id=str(t2.get("id", "")),
                        name=t2.get("name", "Team 2"),
                        abbrev=(t2.get("short_name") or t2.get("name", ""))[:4],
                        sport=Sport.CS2)
        tourney = m.get("tournament", {}) or {}
        return GameContext(
            game_id=str(m.get("id", "")), sport=Sport.CS2,
            status=_game_status(m.get("status", "")),
            home_team=home, away_team=away,
            game_date=local_dt.strftime("%Y-%m-%d"),
            game_time=_fmt_time(local_dt),
            venue=tourney.get("name", "") or "",
            best_of=_i(m.get("best_of"), 3),
        )

    def get_game_context(self, game_id: str, sport: Sport) -> GameContext:
        """GOAT tier: lineups + injuries + season stats hydration.
        UNCHANGED by the Kalshi integration — always receives a real
        BDL game_id (Kalshi-matching happens one layer up, in
        get_games_for_date), so this method's behavior is identical
        to before Kalshi existed."""
        if sport == Sport.CS2:
            return self._cs2_game_context(game_id)

        """Lineup data is only posted once a game begins (BallDontLie
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

        # WNBA DISCOVERY (2026-07-13, live 404): BDL's WNBA API has no
        # /lineups endpoint at all — that's an NBA/MLB feature. Treat a
        # failed lineups call (404 or otherwise) as "no lineup posted"
        # instead of failing the whole prediction; the active-roster
        # fallback below fills the rotation with real players.
        try:
            lineup_data = self._get(sport, "lineups",
                                    params={"game_ids[]": game_id})
            self._apply_lineup(shell, lineup_data.get("data", []), sport)
        except requests.RequestException:
            pass   # no lineups available for this sport/game

        # Live lineup wasn't posted (or the sport has no lineups
        # endpoint) -> use real roster names instead of fully
        # synthetic placeholders. MLB checks for batters; basketball
        # checks for an empty roster (basketball rosters come ONLY
        # from lineups, so without this, WNBA would simulate a single
        # synthetic "Team Avg" player).
        for team in (shell.home_team, shell.away_team):
            if sport == Sport.MLB:
                needs_fallback = not any(not p.is_pitcher
                                         for p in team.roster)
            else:
                needs_fallback = not team.roster
            if needs_fallback:
                self._apply_active_roster_fallback(team, sport)

        if sport == Sport.MLB:
            # PROBABLE STARTERS (added 2026-07-12): pre-game, the BDL
            # lineups endpoint is empty by design, so confirmed_starter
            # was always None until first pitch -> every pre-game
            # prediction ran on rotation avg. MLB's free StatsAPI
            # publishes probable pitchers days ahead — pull them and
            # slot in as the starter. A live lineup (above) always
            # wins over a probable if both exist.
            if (shell.home_team.confirmed_starter is None
                    or shell.away_team.confirmed_starter is None):
                try:
                    self._apply_probable_starters(shell)
                except Exception:
                    pass   # any failure -> rotation-avg fallback, as before

        self._hydrate_team_stats(shell.home_team, sport)
        self._hydrate_team_stats(shell.away_team, sport)

        if sport == Sport.WNBA:
            # WNBA path (all VERIFIED live 2026-07-13): real injuries
            # from player_injuries, and season averages derived by
            # aggregating actual game logs from player_stats — the
            # NBA season_averages/stats-advanced endpoints don't exist
            # on the WNBA API at all.
            for team in (shell.home_team, shell.away_team):
                try:
                    self._apply_wnba_injuries(team)
                except requests.RequestException:
                    pass   # injuries are a bonus signal, never fatal
            try:
                self._hydrate_wnba_players(
                    shell.home_team.roster + shell.away_team.roster)
            except requests.RequestException:
                pass   # players keep defaults -> minutes-flat fallback
        else:
            for p in shell.home_team.roster + shell.away_team.roster:
                self._hydrate_player_stats(p, sport)

        return shell

    def _apply_wnba_injuries(self, team: TeamData):
        """VERIFIED live 2026-07-13: GET /wnba/v1/player_injuries
        ?team_ids[]=X returns [{player:{id,...}, status:'Day-To-Day',
        return_date, comment}]. Map onto the roster via the existing
        _INJURY_MAP so the LOCKED injury-weighting system runs on real
        WNBA availability (e.g. Reese questionable = reduced capacity,
        never removed from the roster)."""
        data = self._get(Sport.WNBA, "player_injuries",
                         params={"team_ids[]": team.team_id})
        status_by_pid = {}
        for row in data.get("data", []):
            pid = str((row.get("player") or {}).get("id", ""))
            if pid:
                status_by_pid[pid] = _injury_status(row.get("status", ""))
        if not status_by_pid:
            return
        for p in team.roster:
            if p.player_id in status_by_pid:
                p.injury_status = status_by_pid[p.player_id]

    def _hydrate_wnba_players(self, players: list):
        """Season averages derived from real game logs — the WNBA
        equivalent of what season_averages does for NBA.

        VERIFIED live 2026-07-13: GET /wnba/v1/player_stats
        ?seasons[]=Y&player_ids[]=A&player_ids[]=B... returns one row
        per player per game with pts/ast/reb/min (min is a string like
        '33'; blk/turnover/pf can be null). Cursor pagination via
        meta.next_cursor. One batched, paginated query covers both
        rosters (~20 players x ~23 games = a few pages).

        usage_rate is set to pts-per-minute so the sim's usage-share
        formula (usage x minutes) distributes team points in proportion
        to each player's REAL scoring — this is what separates Angel
        Reese's projection from the 10th player on the bench."""
        if not players:
            return
        by_pid = {p.player_id: p for p in players}
        totals: dict = {}   # pid -> {'g':n,'pts':x,'ast':x,'reb':x,'min':x}

        params = {"seasons[]": self.season, "per_page": 100,
                  "player_ids[]": list(by_pid.keys())}
        cursor = None
        for _ in range(12):   # safety cap on pagination
            if cursor is not None:
                params["cursor"] = cursor
            data = self._get(Sport.WNBA, "player_stats", params=params)
            for row in data.get("data", []):
                pid = str((row.get("player") or {}).get("id", ""))
                if pid not in by_pid:
                    continue
                t = totals.setdefault(
                    pid, {"g": 0, "pts": 0.0, "ast": 0.0,
                          "reb": 0.0, "min": 0.0})
                mins = _f(row.get("min"))
                if mins <= 0:
                    continue   # DNP rows don't dilute averages
                t["g"] += 1
                t["pts"] += _f(row.get("pts"))
                t["ast"] += _f(row.get("ast"))
                t["reb"] += _f(row.get("reb"))
                t["min"] += mins
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor:
                break

        for pid, t in totals.items():
            if t["g"] == 0:
                continue
            p = by_pid[pid]
            g = t["g"]
            p.games_played = g
            p.ppg = round(t["pts"] / g, 1)
            p.apg = round(t["ast"] / g, 1)
            p.rpg = round(t["reb"] / g, 1)
            p.minutes_proj = round(t["min"] / g, 1)
            if p.minutes_proj > 0:
                p.usage_rate = round(p.ppg / p.minutes_proj, 3)
            p.data_source = "recent"

    def _cs2_game_context(self, match_id: str) -> GameContext:
        """CS2 game context: fetch the match, then hydrate both teams'
        round-win% AND their players' per-map prop averages from the
        SAME finished-match history within the current tournament (one
        combined pass — see _hydrate_cs2_team_and_players)."""
        data = self._get(Sport.CS2, f"matches/{match_id}")
        m = data.get("data", data)
        shell = self._parse_cs2_match_shell(m)
        if shell is None:
            raise ValueError(
                f"CS2 match {match_id} has no teams assigned yet "
                f"(bracket slot not yet determined)")

        tourney_id = (m.get("tournament") or {}).get("id")
        for team in (shell.home_team, shell.away_team):
            try:
                self._hydrate_cs2_team_and_players(
                    team, tourney_id, match_id)
            except requests.RequestException:
                pass   # keep league-avg defaults (LOCKED fail-safe pattern)
        return shell

    def _hydrate_cs2_team_and_players(self, team: TeamData,
                                      tournament_id, exclude_match_id: str):
        """Team round-win% AND player prop averages, derived together
        from ONE pass over the team's recent finished matches in the
        SAME tournament — combined specifically to avoid doubling the
        API-call count (adding player_match_stats as a separate full
        pass risked re-hitting the Run Prediction timeout fixed
        2026-07-13). Capped at 4 matches (down from the team-only
        version's 6) since each match now costs 2 calls instead of 1.

        SCOPE NOTE (honest, not a bug): same-tournament-only, for the
        same reason as before — CS2's generic /matches list ignores
        every filter (VERIFIED live), so cross-tournament history
        isn't reachable yet.

        Round win% = rounds won / rounds played across completed maps
        (match_maps team1_score/team2_score = ROUND counts).
        Player props = per-map rate, derived by dividing each match's
        player_match_stats TOTALS by that match's map count (VERIFIED
        live: player_match_stats returns one row per player with
        match-total kills/deaths/adr/rating/etc, not per-map) —
        counting stats (kills/deaths/assists) are summed then divided
        by maps; rate stats (adr/rating/headshot_pct) are averaged
        directly since they don't accumulate the way kills do."""
        if not tournament_id:
            return
        m_data = self._get(Sport.CS2, "matches",
                           params={"tournament_ids[]": tournament_id,
                                  "per_page": 100})
        team_match_ids = []
        for m in m_data.get("data", []):
            if str(m.get("id", "")) == str(exclude_match_id):
                continue   # never leak the match being predicted
            if _game_status(m.get("status", "")) != GameStatus.FINAL:
                continue
            t1, t2 = m.get("team1") or {}, m.get("team2") or {}
            if (str(t1.get("id", "")) == team.team_id
                    or str(t2.get("id", "")) == team.team_id):
                team_match_ids.append(str(m.get("id", "")))

        if not team_match_ids:
            return   # no in-tournament history yet — keep league avg

        rounds_won = rounds_played = maps_seen = 0
        # player_id -> accumulator dict
        player_totals: dict = {}

        for mid in team_match_ids[:4]:
            try:
                maps_data = self._get(Sport.CS2, "match_maps",
                                      params={"match_ids[]": mid})
            except requests.RequestException:
                continue
            match_maps_played = 0
            for mp in maps_data.get("data", []):
                winner = mp.get("winner") or {}
                t1s, t2s = _i(mp.get("team1_score")), _i(mp.get("team2_score"))
                if t1s == 0 and t2s == 0:
                    continue   # unplayed/no data
                is_winner = str(winner.get("id", "")) == team.team_id
                # team round count is the higher score if they won the
                # map, the lower score if they lost it (CS2 has no
                # draws) — winner isn't labeled team1/team2 directly.
                own_rounds = max(t1s, t2s) if is_winner else min(t1s, t2s)
                rounds_won += own_rounds
                rounds_played += t1s + t2s
                maps_seen += 1
                match_maps_played += 1

            if match_maps_played == 0:
                continue

            try:
                pstats_data = self._get(Sport.CS2, "player_match_stats",
                                        params={"match_id": mid})
            except requests.RequestException:
                continue
            for row in pstats_data.get("data", []):
                if str(row.get("team_id", "")) != team.team_id:
                    continue   # only this team's players from this match
                player = row.get("player") or {}
                pid = str(player.get("id", ""))
                if not pid:
                    continue
                acc = player_totals.setdefault(pid, {
                    "name": (player.get("nickname")
                            or player.get("full_name") or pid),
                    "kills": 0.0, "deaths": 0.0, "assists": 0.0,
                    "adr_sum": 0.0, "rating_sum": 0.0, "hs_sum": 0.0,
                    "matches": 0,
                })
                # Counting stats: per-map rate = match total / maps
                # played in THIS match (not summed raw across matches).
                acc["kills"] += _f(row.get("kills")) / match_maps_played
                acc["deaths"] += _f(row.get("deaths")) / match_maps_played
                acc["assists"] += _f(row.get("assists")) / match_maps_played
                # Rate stats: already per-match rates, just average
                # across matches directly (don't divide by maps again).
                acc["adr_sum"] += _f(row.get("adr"))
                acc["rating_sum"] += _f(row.get("rating"))
                acc["hs_sum"] += _f(row.get("headshot_percentage"))
                acc["matches"] += 1

        # ── Team round-win% (unchanged logic, same shrinkage) ────────
        if rounds_played > 0:
            raw_pct = rounds_won / rounds_played
            if maps_seen < config.CS2_MIN_MAPS_SAMPLE:
                weight = maps_seen / config.CS2_MIN_MAPS_SAMPLE
                raw_pct = (raw_pct * weight
                          + config.CS2_LEAGUE_AVG_ROUND_WIN_PCT * (1 - weight))
            team.cs2_round_win_pct = round(raw_pct, 3)
            team.cs2_maps_sample = maps_seen

        # ── Player prop averages ─────────────────────────────────────
        for pid, acc in player_totals.items():
            n = acc["matches"]
            if n == 0:
                continue
            team.roster.append(PlayerStats(
                player_id=pid, name=acc["name"], sport=Sport.CS2,
                cs2_kills_avg=round(acc["kills"] / n, 2),
                cs2_deaths_avg=round(acc["deaths"] / n, 2),
                cs2_assists_avg=round(acc["assists"] / n, 2),
                cs2_adr_avg=round(acc["adr_sum"] / n, 1),
                cs2_rating_avg=round(acc["rating_sum"] / n, 2),
                cs2_headshot_pct_avg=round(acc["hs_sum"] / n, 1),
                cs2_maps_sample=maps_seen,
            ))

    def _apply_active_roster_fallback(self, team: TeamData, sport: Sport):
        """Real-roster fallback tier for when the live lineup isn't
        posted (MLB pre-game) or the sport has no lineups endpoint at
        all (WNBA). Pulls the team's active players (real names) so
        the sim runs on real identities. Stats still hydrate normally
        afterward via _hydrate_player_stats (recent -> career ->
        team_avg chain).

        MLB: first 9 non-pitchers into lineup spots 1-9.
        Basketball (NBA/WNBA): first 10 players as the rotation —
        real per-game averages from hydration drive usage/minutes,
        so API order here doesn't skew the sim.

        VERIFY (WNBA): 'players/active' is confirmed for MLB; if the
        WNBA API lacks it, we retry the plain 'players' endpoint with
        the same team filter before giving up."""
        data = None
        for path in ("players/active", "players"):
            try:
                data = self._get(sport, path,
                                 params={"team_ids[]": team.team_id})
                break
            except requests.RequestException:
                continue
        if data is None:
            return   # falls through to the fully synthetic placeholder
                     # fallback already built into the engines

        players = [p for p in data.get("data", [])
                  if str(p.get("team", {}).get("id", "")) == team.team_id]
        if not players:
            return

        if sport == Sport.MLB:
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
        else:
            # ALL active players, not an arbitrary first-N slice — the
            # API's ordering isn't by importance (Reese wasn't first on
            # the Dream's list), and hydrated real minutes/usage already
            # weight the rotation correctly; deep-bench players with
            # tiny minutes barely register in the usage shares.
            for p in players:
                pid = str(p.get("id", ""))
                name = (f"{p.get('first_name','')} "
                       f"{p.get('last_name','')}").strip() or pid
                team.roster.append(PlayerStats(
                    player_id=pid, name=name, sport=sport))

    # ── probable starters (MLB StatsAPI) ─────────────────────────────
    def _apply_probable_starters(self, shell: GameContext):
        """Fill confirmed_starter from MLB's free StatsAPI probable
        pitchers (published days ahead — no API key required).

        VERIFIED against a live response (2026-07-12): every game in
        GET https://statsapi.mlb.com/api/v1/schedule
            ?sportId=1&date=YYYY-MM-DD&hydrate=probablePitcher
        carries teams.home.probablePitcher / teams.away.probablePitcher
        as {"id": <mlb_id>, "fullName": "..."}, plus team.name
        ("Washington Nationals") and team.abbreviation ("WSH") for
        matching against the BDL game shell.

        SAFETY RULE: only set confirmed_starter when the probable can
        also be matched to a BallDontLie player id — the sim needs the
        pitcher's BDL season stats, and a starter with no stats behind
        it silently degrades the projection. No BDL match -> keep the
        rotation-avg fallback (LOCKED behavior).

        A probable is a plan, not a lineup: if the BDL live lineup has
        already set confirmed_starter, that always wins (checked at the
        call site and re-checked per team here)."""
        resp = requests.get(
            "https://statsapi.mlb.com/api/v1/schedule",
            params={"sportId": 1, "date": shell.game_date,
                    "hydrate": "probablePitcher"},
            timeout=15)
        resp.raise_for_status()
        dates = resp.json().get("dates", [])
        games = dates[0].get("games", []) if dates else []

        match = self._match_statsapi_game(shell, games)
        if not match:
            return

        for side, team in (("home", shell.home_team),
                           ("away", shell.away_team)):
            if team.confirmed_starter is not None:
                continue   # live lineup already set it — that wins
            prob = (match.get("teams", {}).get(side, {})
                    .get("probablePitcher") or {})
            full_name = (prob.get("fullName") or "").strip()
            if not full_name:
                continue   # no probable announced for this side yet
            bdl_id = self._find_bdl_player_id(full_name, team.team_id)
            if not bdl_id:
                continue   # SAFETY RULE above — rotation avg instead
            sp = PlayerStats(
                player_id=bdl_id, name=full_name, sport=Sport.MLB,
                is_pitcher=True, is_starter=True)
            team.confirmed_starter = sp
            team.roster.append(sp)

    def _match_statsapi_game(self, shell: GameContext,
                             games: list) -> dict | None:
        """Match the BDL game shell to its MLB StatsAPI schedule entry
        by team names (primary) or abbreviations (fallback), matching
        home-to-home so a doubleheader's reversed listings can't
        cross-wire. If a doubleheader yields two candidates, prefer the
        one that actually has a probable pitcher posted."""
        def norm(s):
            return (s or "").strip().lower()

        candidates = []
        for g in games:
            th = g.get("teams", {}).get("home", {}).get("team", {})
            ta = g.get("teams", {}).get("away", {}).get("team", {})
            home_ok = (norm(th.get("name")) == norm(shell.home_team.name)
                       or norm(th.get("abbreviation"))
                       == norm(shell.home_team.abbrev))
            away_ok = (norm(ta.get("name")) == norm(shell.away_team.name)
                       or norm(ta.get("abbreviation"))
                       == norm(shell.away_team.abbrev))
            if home_ok and away_ok:
                candidates.append(g)

        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        # doubleheader: prefer an entry with probables actually posted
        for g in candidates:
            teams = g.get("teams", {})
            if (teams.get("home", {}).get("probablePitcher")
                    or teams.get("away", {}).get("probablePitcher")):
                return g
        return candidates[0]

    def _find_bdl_player_id(self, full_name: str,
                            team_id: str) -> str | None:
        """Resolve an MLB StatsAPI pitcher name to a BallDontLie player
        id so season stats can hydrate through the normal chain.

        VERIFY on first live pull: the BDL players endpoint's 'search'
        param is documented for NBA; assuming MLB mirrors it (same
        pattern as every other shared endpoint in this API). Prefers a
        player on the expected team; falls back to an exact full-name
        match on any team (mid-season trades can lag team data)."""
        try:
            data = self._get(Sport.MLB, "players",
                             params={"search": full_name.split()[-1]})
        except requests.RequestException:
            return None

        exact_any_team = None
        for p in data.get("data", []):
            name = (f"{p.get('first_name', '')} "
                    f"{p.get('last_name', '')}").strip()
            if name.lower() != full_name.lower():
                continue
            pid = str(p.get("id", ""))
            if str(p.get("team", {}).get("id", "")) == team_id:
                return pid
            if exact_any_team is None:
                exact_any_team = pid
        return exact_any_team

    def get_live_scores(self, sport: Sport) -> list[dict]:
        """FREE tier for schedule/score; GOAT unlocks true live box
        score granularity, but the base games endpoint updates scores
        in near-real-time already, which covers the ticker's needs.
        UNCHANGED by Kalshi — the live ticker stays BDL-sourced (score
        updates need low latency; Kalshi's market prices, not scores,
        are the enrichment layer, applied at the schedule level only)."""
        if sport == Sport.CS2:
            return self._cs2_live_scores()

        """'Today' means the LOCAL calendar date — not the server's clock.
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

    def _cs2_live_scores(self) -> list[dict]:
        """CS2 live ticker — same tournament-scoping constraint as
        _cs2_matches_for_date (the generic /matches list is stale and
        ignores filters). Pulls today's matches through current
        tournaments and reports live map score (maps won so far)."""
        today_local = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        try:
            t_data = self._get(Sport.CS2, "tournaments",
                               params={"per_page": 100})
        except requests.RequestException:
            return []
        current_ids = [t.get("id") for t in t_data.get("data", [])
                       if t.get("status") == "current"]

        out = []
        for tid in current_ids:
            try:
                m_data = self._get(Sport.CS2, "matches",
                                   params={"tournament_ids[]": tid,
                                          "per_page": 100})
            except requests.RequestException:
                continue
            for m in m_data.get("data", []):
                local_dt = _parse_local_dt(m.get("start_time", ""))
                if local_dt is None or local_dt.strftime("%Y-%m-%d") != today_local:
                    continue
                t1, t2 = m.get("team1") or {}, m.get("team2") or {}
                status = _game_status(m.get("status", ""))
                out.append({
                    "game_id": str(m.get("id", "")),
                    "status": status.value,
                    "home": (t1.get("short_name") or t1.get("name", "TM1"))[:4],
                    "away": (t2.get("short_name") or t2.get("name", "TM2"))[:4],
                    "home_score": _i(m.get("team1_score")),
                    "away_score": _i(m.get("team2_score")),
                    "period": status.value.upper(),
                })
        return out

    def _fetch_game_player_stats(self, game_id: str,
                                 sport: Sport) -> list:
        """Player stat rows for one game. MLB/NBA use /stats;
        the WNBA API has no /stats route — player rows live at
        /player_stats (VERIFIED live 2026-07-13), cursor-paginated."""
        if sport != Sport.WNBA:
            data = self._get(sport, "stats",
                             params={"game_ids[]": game_id})
            return data.get("data", [])
        rows, cursor = [], None
        params = {"game_ids[]": game_id, "per_page": 100}
        for _ in range(5):
            if cursor is not None:
                params["cursor"] = cursor
            data = self._get(sport, "player_stats", params=params)
            rows.extend(data.get("data", []))
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
        return rows

    def get_final_boxscore(self, game_id: str, sport: Sport) -> dict:
        """GOAT tier — Game Player Stats -> settling pipeline input.
        Returns _parse_boxscore output, which includes "status" — the
        last-out guard in /api/settle depends on that key."""
        if sport == Sport.CS2:
            return self._cs2_boxscore(game_id)
        game = self._get(sport, f"games/{game_id}").get("data", {})
        stat_rows = self._fetch_game_player_stats(game_id, sport)
        return self._parse_boxscore(game, stat_rows, sport)

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
        if sport == Sport.CS2:
            box = self._cs2_boxscore(game_id)
            box["line_score"] = box.pop("_cs2_map_line_score", [])
            box["period"] = box.pop("_cs2_period_label", "")
            return box
        game = self._get(sport, f"games/{game_id}").get("data", {})
        stat_rows = self._fetch_game_player_stats(game_id, sport)
        box = self._parse_boxscore(game, stat_rows, sport)

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

    def _cs2_boxscore(self, match_id: str) -> dict:
        """CS2 settle/display data — MAPS won per team (the settleable
        unit), not rounds. VERIFIED shape: match_maps rows carry
        team1_score/team2_score as ROUND counts per map (e.g. 13-1)
        and a winner block with a team id — map WINS are derived by
        counting how many maps each team's id appears as winner on,
        not by comparing home/away labels (CS2 has no home/away, only
        team1/team2, and match_maps doesn't echo the match's team1/
        team2 order directly)."""
        m_data = self._get(Sport.CS2, f"matches/{match_id}")
        m = m_data.get("data", m_data)
        t1, t2 = m.get("team1") or {}, m.get("team2") or {}
        t1_id, t2_id = str(t1.get("id", "")), str(t2.get("id", ""))

        maps_data = self._get(Sport.CS2, "match_maps",
                              params={"match_ids[]": match_id})
        maps = maps_data.get("data", [])

        home_maps = away_maps = 0
        line_score = []
        for mp in sorted(maps, key=lambda x: x.get("map_number", 0)):
            winner_id = str((mp.get("winner") or {}).get("id", ""))
            if winner_id == t1_id:
                home_maps += 1
            elif winner_id == t2_id:
                away_maps += 1
            line_score.append({
                "label": mp.get("map_name", f"Map {mp.get('map_number')}"),
                "home": _i(mp.get("team1_score")),
                "away": _i(mp.get("team2_score")),
            })

        # Player props (added when player totals were reinstated,
        # 2026-07-13): player_match_stats(match_id) returns exactly
        # the unit the sim projects — kills/deaths/assists as MATCH
        # totals, adr/rating/headshot_pct as match-level rates already
        # averaged across whatever maps were played. No transform
        # needed; this is the same shape used to build projections.
        player_stats = {}
        try:
            pstats_data = self._get(Sport.CS2, "player_match_stats",
                                    params={"match_id": match_id})
            for row in pstats_data.get("data", []):
                player = row.get("player") or {}
                pid = str(player.get("id", ""))
                if not pid:
                    continue
                row_team_id = str(row.get("team_id", ""))
                team_tag = ("home" if row_team_id == t1_id
                           else "away" if row_team_id == t2_id else "")
                player_stats[pid] = {
                    "kills": _f(row.get("kills")),
                    "deaths": _f(row.get("deaths")),
                    "assists": _f(row.get("assists")),
                    "adr": _f(row.get("adr")),
                    "rating": _f(row.get("rating")),
                    "headshot_pct": _f(row.get("headshot_percentage")),
                    "_name": (player.get("nickname")
                             or player.get("full_name") or pid),
                    "_team": team_tag,
                }
        except requests.RequestException:
            pass   # settle still proceeds on maps/score; props stay empty

        return {
            "home_score": home_maps, "away_score": away_maps,
            "status": _game_status(m.get("status", "")).value,
            "player_stats": player_stats,
            "_cs2_map_line_score": line_score,
            "_cs2_period_label": m.get("status", ""),
        }

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
        all 30 teams), so match by team_id client-side instead.

        WNBA (VERIFIED live 2026-07-13): no advanced-stats or team
        season-stats endpoints exist, but the games endpoint returns
        every game with final scores — so ORtg/DRtg are derived from
        real points scored/allowed relative to league-average scoring,
        and pace is proxied from combined scoring. Blowout-resistant
        enough at 20+ games, and it's what actually moves the win
        probability off 50/50."""
        if sport == Sport.WNBA:
            try:
                data = self._get(sport, "games",
                                 params={"seasons[]": self.season,
                                        "team_ids[]": team.team_id,
                                        "per_page": 100})
            except requests.RequestException:
                return
            pf = pa = games = 0
            for g in data.get("data", []):
                if _game_status(g.get("status", "")) != GameStatus.FINAL:
                    continue
                home_j = g.get("home_team", {})
                is_home = str(home_j.get("id", "")) == team.team_id
                hs = _i(g.get("home_score"))
                as_ = _i(g.get("away_score"))
                if hs == 0 and as_ == 0:
                    continue   # bad/empty row
                pf += hs if is_home else as_
                pa += as_ if is_home else hs
                games += 1
            if games < 3:
                return   # too small a sample — keep league-avg defaults
            avg_ppg = getattr(config, "LEAGUE_AVG_PPG_WNBA", 81.0)
            pf_pg, pa_pg = pf / games, pa / games
            team.team_ortg = round(
                config.LEAGUE_AVG_ORTG_WNBA * (pf_pg / avg_ppg), 1)
            team.team_drtg = round(
                config.LEAGUE_AVG_DRTG_WNBA * (pa_pg / avg_ppg), 1)
            # pace proxy: combined scoring vs league combined scoring
            team.team_pace = round(
                config.LEAGUE_AVG_PACE_WNBA
                * ((pf_pg + pa_pg) / (2 * avg_ppg)), 1)
            return

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

        # Team ids/abbrevs so player rows can carry a "_team" tag —
        # lets the frontend group the box score by team instead of
        # one mixed list.
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
                # Underscore prefix = metadata, not a stat. The settle
                # pipeline skips keys starting with "_".
                out["_name"] = name
                team_ref = row.get("team") or player.get("team") or {}
                row_team_id = str(team_ref.get("id", ""))
                if row_team_id == home_id:
                    out["_team"] = home_abbrev
                elif row_team_id == away_id:
                    out["_team"] = away_abbrev
                else:
                    out["_team"] = team_ref.get("abbreviation", "")
                player_stats[pid] = out

        # SETTLE STATUS FIX (2026-07-12): the last-out guard in
        # /api/settle checks box["status"] before settling — without
        # this key every settle 409s, and before the guard existed the
        # missing status let an in-progress 0-0 boxscore settle as if
        # final (the NYY@WSH incident).
        return {"home_score": home_score, "away_score": away_score,
                "status": _game_status(game.get("status", "")).value,
                "player_stats": player_stats}
