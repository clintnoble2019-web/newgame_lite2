"""
NexGame Lite — NBA Simulation Engine
Kage Software · 2026

One iteration = one full game simulated QUARTER BY QUARTER (LOCKED).
    - Possession-by-possession, pace-adjusted per team
    - Points sampled from ORtg vs opponent DRtg
    - Player points distributed by usage share
    - Foul trouble -> minutes reduction
    - Overtime: 5-minute periods until a winner
    - Garbage time simulated normally — all points count (LOCKED)
    - Injured players stay on roster, weighted by probability (LOCKED)
"""

import random
import config
from models import GameContext, PlayerStats, IterationResult, Sport


def _league_params(sport) -> dict:
    """WNBA plays 4x10-minute quarters (40-min regulation) in a lower
    scoring environment; NBA plays 4x12 (48-min). Pace stats are
    per-regulation-game in both conventions, so possessions per period
    scale by minutes/regulation. PPP normalizes against the correct
    league-average DRtg or cross-league ratings skew every projection."""
    if sport == Sport.WNBA:
        return {"quarter_min": 10, "regulation_min": 40,
                "avg_ortg": config.LEAGUE_AVG_ORTG_WNBA,
                "avg_drtg": config.LEAGUE_AVG_DRTG_WNBA,
                "avg_pace": config.LEAGUE_AVG_PACE_WNBA}
    return {"quarter_min": 12, "regulation_min": 48,
            "avg_ortg": config.LEAGUE_AVG_ORTG,
            "avg_drtg": config.LEAGUE_AVG_DRTG,
            "avg_pace": config.LEAGUE_AVG_PACE}


def _availability_roll(player: PlayerStats, rng: random.Random) -> float:
    w = config.INJURY_WEIGHTS[player.injury_status.value]
    if rng.random() < w["plays"]:
        return w["capacity"]
    return 0.0


def _active_rotation(team, rng: random.Random, lp: dict) -> list[tuple]:
    """Roll injury availability, then build a REAL rotation instead of
    just including every available roster player on equal footing.

    Previously every healthy player entered scoring-credit with equal
    structural standing — a 10th-man scrub and a 35-minute starter
    were only differentiated by raw usage_rate. Real teams only give
    meaningful burn to ~8-10 players; the rest is mop-up time that
    barely shows up in a box score. Two real fixes here:
      1. Cap the rotation to NBA_ROTATION_SIZE, ranked by minutes_proj
         (deep bench falls out entirely — they weren't scoring
         meaningfully anyway, and including them just diluted shares).
      2. Normalize the survivors' minutes to the REAL team minute
         budget (5 players x regulation_min = 240 NBA / 200 WNBA).
         Raw hydrated minutes_proj values are independent per-player
         season averages that won't necessarily sum correctly on
         their own — normalizing guarantees the on-court total is
         always right, while preserving each player's relative share.

    Returns [(player, capacity, minutes_share)] — minutes_share is
    this player's normalized per-game minutes, consumed by _quarter
    to scale their scoring-credit weight per period actually played.
    """
    available = []
    for p in team.roster:
        cap = _availability_roll(p, rng)
        if cap > 0:
            available.append((p, cap))
    if not available:                      # extreme edge: everyone out
        filler = PlayerStats(
            player_id=f"{team.team_id}_avg", name=f"{team.abbrev} Avg",
            sport=team.sport, ppg=10.0, usage_rate=0.20,
            minutes_proj=24, data_source="team_avg")
        return [(filler, 1.0, float(lp["regulation_min"]) / 5)]

    def raw_minutes(p):
        # NOTE: no "or default" fallback here on purpose. A genuinely
        # hydrated 0.0 (player truly doesn't play, or roster-fallback
        # players who haven't hydrated minutes yet) must sort to the
        # BOTTOM and correctly fall out of the rotation cap — an
        # earlier version of this used `p.minutes_proj or DEFAULT`,
        # which treated real zeros as "missing" and promoted them
        # above legitimate bench players with actual minutes. Caught
        # in testing before deploy (2026-07-14).
        return p.minutes_proj

    available.sort(key=lambda pc: raw_minutes(pc[0]), reverse=True)
    rotation = available[:config.NBA_ROTATION_SIZE]

    total_raw = sum(raw_minutes(p) for p, cap in rotation)
    team_minute_budget = lp["regulation_min"] * 5   # 5 players on court
    if total_raw <= 0:
        # Extreme edge: nobody in the capped rotation has hydrated
        # minutes at all. Split the real team budget evenly rather
        # than crash on a divide-by-zero or silently favor zeros.
        equal_share = team_minute_budget / len(rotation)
        return [(p, cap, equal_share) for p, cap in rotation]
    return [
        (p, cap, raw_minutes(p) / total_raw * team_minute_budget)
        for p, cap in rotation
    ]


def _points_per_possession(ortg: float, opp_drtg: float,
                           lp: dict) -> float:
    """Expected PPP = own ORtg adjusted by opponent defense, normalized
    against the correct league's average DRtg (NBA vs WNBA)."""
    ortg = ortg or lp["avg_ortg"]
    opp_drtg = opp_drtg or lp["avg_drtg"]
    return (ortg / 100.0) * (opp_drtg / lp["avg_drtg"])


def _sample_possession(ppp: float, rng: random.Random) -> int:
    """One possession -> 0/1/2/3 points.
    Outcome probabilities tuned so the mean equals ppp with realistic
    variance (empty possessions ~ half, threes ~ a third of makes)."""
    # Solve simple mixture: P(3)=a, P(2)=b, P(1)=c, mean = 3a+2b+c = ppp
    a = ppp * 0.115          # threes
    b = ppp * 0.295          # twos
    c = ppp * 0.065          # single FT points
    roll = rng.random()
    if roll < a:
        return 3
    if roll < a + b:
        return 2
    if roll < a + b + c:
        return 1
    return 0


def _quarter(off_rotation: list, ortg: float, opp_drtg: float,
             pace: float, minutes: float, foul_trouble: set,
             stat_lines: dict, rng: random.Random, lp: dict,
             team_abbrev: str = "") -> int:
    """One team's offensive output for one period."""
    possessions = max(8, int(rng.gauss(
        (pace or lp["avg_pace"])
        * (minutes / lp["regulation_min"]), 1.8)))
    ppp = _points_per_possession(ortg, opp_drtg, lp)

    # Scoring-credit shares: usage (touches) x minutes actually played
    # THIS period (from the rotation's normalized per-game share,
    # scaled to this period's fraction of the game) x shooting
    # efficiency (true_shooting vs league average) x availability.
    # Efficiency weighting means a low-usage, high-efficiency player
    # gets proportionally MORE credit than raw usage alone would give
    # them, and vice versa — previously a 38% shooter and a 60% TS
    # player with equal usage were credited identically.
    shares = []
    for p, cap, min_share in off_rotation:
        minutes_this_period = min_share * (minutes / lp["regulation_min"])
        efficiency_factor = ((p.true_shooting or config.LEAGUE_AVG_TS)
                             / config.LEAGUE_AVG_TS)
        share = ((p.usage_rate or 0.18) * minutes_this_period
                * cap * efficiency_factor)
        if p.player_id in foul_trouble:
            share *= 0.55                 # foul trouble: benched earlier
        shares.append(share)
    total_share = sum(shares) or 1.0

    pts = 0
    for _ in range(possessions):
        scored = _sample_possession(ppp, rng)
        if scored:
            pts += scored
            # credit a player by (usage x minutes x efficiency) share
            roll = rng.random() * total_share
            cum = 0.0
            for (p, cap, min_share), share in zip(off_rotation, shares):
                cum += share
                if roll <= cum:
                    sl = stat_lines.setdefault(
                        p.player_id,
                        {"name": p.name, "_team": team_abbrev, "points": 0,
                         "assists": 0, "rebounds": 0})
                    sl["points"] += scored
                    break
    return pts


def _accumulate_hustle(rotation: list, stat_lines: dict,
                       minutes: float, rng: random.Random, lp: dict,
                       team_abbrev: str = ""):
    """Assists + rebounds per period, scaled from per-game averages
    over the correct regulation length (48 NBA / 40 WNBA). Each
    player's own apg/rpg already implicitly encodes their real
    minutes distribution, so the per-period fraction alone (not
    minutes_share) is the right scale here — minutes_share is unused
    in this function on purpose."""
    frac = minutes / lp["regulation_min"]
    for p, cap, min_share in rotation:
        sl = stat_lines.setdefault(
            p.player_id,
            {"name": p.name, "_team": team_abbrev, "points": 0,
             "assists": 0, "rebounds": 0})
        sl["assists"] += max(0, int(rng.gauss((p.apg or 2.0) * frac * cap, 0.7)))
        sl["rebounds"] += max(0, int(rng.gauss((p.rpg or 3.5) * frac * cap, 0.9)))


def simulate_nba_game(context: GameContext,
                      rng: random.Random) -> IterationResult:
    """ONE iteration. Called 10,000 times by the aggregator."""
    home, away = context.home_team, context.away_team
    lp = _league_params(context.sport)

    home_rot = _active_rotation(home, rng, lp)
    away_rot = _active_rotation(away, rng, lp)

    home_score = away_score = 0
    stat_lines: dict = {}
    periods = []
    foul_trouble: set = set()

    # home court edge: small ORtg bump
    home_ortg = (home.team_ortg or lp["avg_ortg"]) + 1.5
    away_ortg = away.team_ortg or lp["avg_ortg"]
    if context.back_to_back_home:
        home_ortg -= 1.8
    if context.back_to_back_away:
        away_ortg -= 1.8

    for q in range(1, 5):
        h = _quarter(home_rot, home_ortg, away.team_drtg,
                     home.team_pace, lp["quarter_min"], foul_trouble,
                     stat_lines, rng, lp, home.abbrev)
        a = _quarter(away_rot, away_ortg, home.team_drtg,
                     away.team_pace, lp["quarter_min"], foul_trouble,
                     stat_lines, rng, lp, away.abbrev)
        _accumulate_hustle(home_rot, stat_lines, lp["quarter_min"], rng,
                           lp, home.abbrev)
        _accumulate_hustle(away_rot, stat_lines, lp["quarter_min"], rng,
                           lp, away.abbrev)
        home_score += h
        away_score += a
        periods.append({"period": f"Q{q}", "home": h, "away": a})

        # foul trouble check after each quarter (LOCKED decision node)
        for p, cap, min_share in home_rot + away_rot:
            if p.is_starter_nba and rng.random() < 0.04:
                foul_trouble.add(p.player_id)

    # Overtime — 5 min periods until winner (garbage time still normal)
    ot = 1
    while home_score == away_score:
        h = _quarter(home_rot, home_ortg, away.team_drtg,
                     home.team_pace, 5, foul_trouble, stat_lines, rng,
                     lp, home.abbrev)
        a = _quarter(away_rot, away_ortg, home.team_drtg,
                     away.team_pace, 5, foul_trouble, stat_lines, rng,
                     lp, away.abbrev)
        home_score += h
        away_score += a
        periods.append({"period": f"OT{ot}", "home": h, "away": a})
        ot += 1
        if ot > 6:                        # safety valve
            home_score += 1
            break

    winner = "home" if home_score > away_score else "away"
    return IterationResult(home_score, away_score, winner,
                           stat_lines, periods)
