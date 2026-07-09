"""
NexGame Lite — MLB Simulation Engine
Kage Software · 2026

One iteration = one full game simulated INNING BY INNING (LOCKED).
    - Batter-by-batter plate appearances until 3 outs
    - Base state tracked through every at-bat
    - Pitcher fatigue -> starter pulled at pitch count threshold
    - Bullpen takes over with its own (separate) stats
    - Extra innings: runner-on-2nd rule
    - Garbage time simulated normally — all runs count (LOCKED)
    - Injured players stay on roster, weighted by probability (LOCKED)
"""

import random
import config
import engine.mlb_strength as strength
from models import GameContext, PlayerStats, IterationResult, InjuryStatus


def _availability_roll(player: PlayerStats, rng: random.Random) -> float:
    """Injury probability weight (LOCKED). Returns capacity multiplier:
    1.0 full, <1.0 reduced, 0.0 doesn't play this iteration."""
    w = config.INJURY_WEIGHTS[player.injury_status.value]
    if rng.random() < w["plays"]:
        return w["capacity"]
    return 0.0


def _resolve_pitcher(team, rng: random.Random) -> PlayerStats:
    """Starting pitcher decision node (LOCKED):
    confirmed -> individual stats; else Starting Rotation Average
    (bullpen EXCLUDED)."""
    sp = team.confirmed_starter
    if sp is not None and _availability_roll(sp, rng) > 0:
        return sp
    return PlayerStats(
        player_id=f"{team.team_id}_rot_avg",
        name=f"{team.name} Rotation Avg",
        sport=team.sport, is_pitcher=True, is_starter=True,
        era=team.rotation_avg_era or config.LEAGUE_AVG_ERA,
        whip=team.rotation_avg_whip or config.LEAGUE_AVG_WHIP,
        k_per_9=team.rotation_avg_k9 or 8.50,
        data_source="team_avg",
    )


def _bullpen_pitcher(team) -> PlayerStats:
    """Bullpen — always separate from rotation (LOCKED)."""
    return PlayerStats(
        player_id=f"{team.team_id}_bullpen",
        name=f"{team.name} Bullpen",
        sport=team.sport, is_pitcher=True, is_starter=False,
        era=team.bullpen_era or config.LEAGUE_AVG_ERA,
        whip=team.bullpen_whip or config.LEAGUE_AVG_WHIP,
        data_source="team_avg",
    )


def _lineup(team, rng: random.Random) -> list[PlayerStats]:
    """Ordered 1-9 lineup with injury weights applied per iteration.
    A player who rolls 'doesn't play' is replaced by a team-average bat
    — the roster object itself is never modified (LOCKED).

    FALLBACK (LOCKED chain, recent -> team_avg): if the data provider
    hasn't posted a real lineup yet (e.g. BallDontLie only returns
    lineups once a game begins), team.roster may have fewer than 9
    batters — or zero. Pad up to exactly 9 with team-average bats so
    the engine always has a full lineup and never indexes out of
    range. This is the fallback chain working as designed, not a
    special case."""
    batters = sorted(
        [p for p in team.roster if not p.is_pitcher and p.lineup_spot],
        key=lambda p: p.lineup_spot)
    out = []
    for b in batters:
        cap = _availability_roll(b, rng)
        if cap > 0:
            out.append((b, cap))
        else:
            filler = PlayerStats(
                player_id=f"{b.player_id}_repl",
                name=f"{team.abbrev} Bench",
                sport=team.sport,
                obp=team.team_obp or config.LEAGUE_AVG_OBP,
                slg=team.team_slg or 0.410,
                lineup_spot=b.lineup_spot,
                data_source="team_avg")
            out.append((filler, 1.0))

    # Pad to exactly 9 spots if the lineup wasn't fully posted (or at
    # all) — team-average fallback bats fill the gap.
    while len(out) < 9:
        spot = len(out) + 1
        filler = PlayerStats(
            player_id=f"{team.team_id}_avg{spot}",
            name=f"{team.abbrev} Batter {spot}",
            sport=team.sport,
            obp=team.team_obp or config.LEAGUE_AVG_OBP,
            slg=team.team_slg or 0.410,
            lineup_spot=spot,
            data_source="team_avg")
        out.append((filler, 1.0))

    return out


def _plate_appearance(batter: PlayerStats, capacity: float,
                      pitcher: PlayerStats, park_factor: float,
                      team_multiplier: float,
                      rng: random.Random) -> str:
    """One PA. Returns: 'out' | 'walk' | 'single' | 'double' | 'triple' | 'hr'.
    Hit probability = batter OBP scaled by pitcher WHIP vs league,
    reduced by injury capacity, THEN scaled by the batting team's
    four-factor strength multiplier (HIT × oppPITCH × oppDEF × SCORE —
    ported from Samurai Picks). This is what actually separates two
    teams that would otherwise both be running on similar fallback
    averages — real team_obp/team_slg/rotation_era differences now
    compound into the per-PA math instead of being ignored."""
    obp = (batter.obp or config.LEAGUE_AVG_OBP) * capacity
    whip = pitcher.whip or config.LEAGUE_AVG_WHIP
    pitcher_factor = whip / config.LEAGUE_AVG_WHIP
    p_reach = min(0.480, max(0.180,
                             obp * (pitcher_factor ** 0.7) * team_multiplier))

    if rng.random() >= p_reach:
        return "out"

    # Reached base: walk vs hit split (~28% of times on base are walks)
    if rng.random() < 0.28:
        return "walk"

    # Hit type weighted by batter power (SLG vs league ~.410)
    power = (batter.slg or 0.410) / 0.410
    base = config.HIT_TYPE_BASE
    weights = {
        "single": base["single"] / power,
        "double": base["double"] * power,
        "triple": base["triple"],
        "hr": base["hr"] * (power ** 1.6) * park_factor,
    }
    total = sum(weights.values())
    roll = rng.random() * total
    cum = 0.0
    for hit_type, w in weights.items():
        cum += w
        if roll <= cum:
            return hit_type
    return "single"


def _advance(bases: list, outcome: str) -> int:
    """Advance base state. bases = [1B, 2B, 3B] occupancy bools.
    Returns runs scored this PA. Simplified but coherent advancement."""
    runs = 0
    if outcome == "walk" or outcome == "single":
        push = 1 if outcome == "walk" else 1
        # walk: forced advance only; single: everyone moves up one (simplified)
        if outcome == "walk":
            if bases[0] and bases[1] and bases[2]:
                runs += 1
            elif bases[0] and bases[1]:
                bases[2] = True
            elif bases[0]:
                bases[1] = True
            bases[0] = True
        else:
            if bases[2]:
                runs += 1
            bases[2], bases[1] = bases[1], bases[0]
            bases[0] = True
    elif outcome == "double":
        if bases[2]:
            runs += 1
        if bases[1]:
            runs += 1
        bases[2] = bases[0]
        bases[1] = True
        bases[0] = False
    elif outcome == "triple":
        runs += sum(bases)
        bases[0] = bases[1] = False
        bases[2] = True
    elif outcome == "hr":
        runs += sum(bases) + 1
        bases[0] = bases[1] = bases[2] = False
    return runs


def _half_inning(lineup: list, spot: int, pitcher: PlayerStats,
                 pitch_count: int, park_factor: float, team_multiplier: float,
                 stat_lines: dict, runner_on_2nd: bool,
                 rng: random.Random) -> tuple[int, int, int]:
    """Simulate one half-inning.
    Returns (runs, next_lineup_spot, updated_pitch_count)."""
    outs = 0
    runs = 0
    bases = [False, runner_on_2nd, False]   # extra-innings rule

    while outs < 3:
        batter, capacity = lineup[spot % 9]
        outcome = _plate_appearance(batter, capacity, pitcher,
                                    park_factor, team_multiplier, rng)
        pitch_count += int(rng.gauss(config.PITCHES_PER_PA, 1.2))

        sl = stat_lines.setdefault(
            batter.player_id, {"name": batter.name, "hits": 0, "rbis": 0})
        if outcome == "out":
            outs += 1
            # pitcher K credit chance scaled by K/9
            k_rate = (pitcher.k_per_9 or 8.5) / 27.0
            if rng.random() < k_rate * 3:
                psl = stat_lines.setdefault(
                    pitcher.player_id,
                    {"name": pitcher.name, "strikeouts": 0})
                psl["strikeouts"] = psl.get("strikeouts", 0) + 1
        else:
            scored = _advance(bases, outcome)
            runs += scored
            if outcome != "walk":
                sl["hits"] += 1
            sl["rbis"] += scored
        spot += 1

    return runs, spot % 9, pitch_count


def simulate_mlb_game(context: GameContext,
                      rng: random.Random) -> IterationResult:
    """ONE iteration. Called 10,000 times by the aggregator."""
    home, away = context.home_team, context.away_team

    home_sp = _resolve_pitcher(home, rng)
    away_sp = _resolve_pitcher(away, rng)
    home_pen = _bullpen_pitcher(home)
    away_pen = _bullpen_pitcher(away)

    home_lineup = _lineup(home, rng)
    away_lineup = _lineup(away, rng)

    # ── Four-factor team strength (ported from Samurai Picks) ────────
    # Computed once per game, not per PA — a team-level lever, not a
    # per-batter one. home_strength answers "how much does the HOME
    # team's batting quality × the AWAY starter's weakness intersect";
    # away_strength is the mirror. This is what separates two teams
    # that would otherwise both look like fallback-average clones.
    home_strength = strength.team_strength(
        home, away_sp.era or config.LEAGUE_AVG_ERA,
        away_sp.whip or config.LEAGUE_AVG_WHIP)
    away_strength = strength.team_strength(
        away, home_sp.era or config.LEAGUE_AVG_ERA,
        home_sp.whip or config.LEAGUE_AVG_WHIP)
    home_multiplier = home_strength["multiplier"]
    away_multiplier = away_strength["multiplier"]

    home_score = away_score = 0
    home_spot = away_spot = 0
    home_pitches = away_pitches = 0
    stat_lines: dict = {}
    periods = []
    inning = 1

    while True:
        extra = inning > 9

        # current pitcher: fatigue check (LOCKED: pull at pitch threshold)
        h_pitcher = home_sp if home_pitches < config.PITCH_COUNT_PULL else home_pen
        a_pitcher = away_sp if away_pitches < config.PITCH_COUNT_PULL else away_pen

        # Top: away bats vs home pitching
        runs, away_spot, home_pitches = _half_inning(
            away_lineup, away_spot, h_pitcher, home_pitches,
            context.park_factor, away_multiplier, stat_lines,
            runner_on_2nd=extra, rng=rng)
        away_score += runs

        # Bottom: home bats (walk-off aware: skip if ahead in 9th+)
        if not (inning >= 9 and home_score > away_score):
            runs, home_spot, away_pitches = _half_inning(
                home_lineup, home_spot, a_pitcher, away_pitches,
                context.park_factor, home_multiplier, stat_lines,
                runner_on_2nd=extra, rng=rng)
            home_score += runs

        periods.append({"inning": inning,
                        "home": home_score, "away": away_score})

        if inning >= 9 and home_score != away_score:
            break
        inning += 1
        if inning > 20:                     # safety valve
            home_score += 1
            break

    winner = "home" if home_score > away_score else "away"
    return IterationResult(home_score, away_score, winner,
                           stat_lines, periods)
