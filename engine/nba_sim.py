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
from models import GameContext, PlayerStats, IterationResult


def _availability_roll(player: PlayerStats, rng: random.Random) -> float:
    w = config.INJURY_WEIGHTS[player.injury_status.value]
    if rng.random() < w["plays"]:
        return w["capacity"]
    return 0.0


def _active_rotation(team, rng: random.Random) -> list[tuple]:
    """Roll injury availability once per iteration.
    Returns [(player, capacity)]. Minutes of unavailable players are
    implicitly redistributed via usage renormalization — roster object
    never modified (LOCKED)."""
    rotation = []
    for p in team.roster:
        cap = _availability_roll(p, rng)
        if cap > 0:
            rotation.append((p, cap))
    if not rotation:                      # extreme edge: everyone out
        rotation = [(PlayerStats(
            player_id=f"{team.team_id}_avg", name=f"{team.abbrev} Avg",
            sport=team.sport, ppg=10.0, usage_rate=0.20,
            minutes_proj=24, data_source="team_avg"), 1.0)]
    return rotation


def _points_per_possession(ortg: float, opp_drtg: float) -> float:
    """Expected PPP = own ORtg adjusted by opponent defense."""
    ortg = ortg or config.LEAGUE_AVG_ORTG
    opp_drtg = opp_drtg or config.LEAGUE_AVG_DRTG
    return (ortg / 100.0) * (opp_drtg / config.LEAGUE_AVG_DRTG)


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
             stat_lines: dict, rng: random.Random) -> int:
    """One team's offensive output for one period."""
    possessions = max(8, int(rng.gauss(
        (pace or config.LEAGUE_AVG_PACE) * (minutes / 48.0), 1.8)))
    ppp = _points_per_possession(ortg, opp_drtg)

    # usage shares (injury capacity + foul trouble reduce share)
    shares = []
    for p, cap in off_rotation:
        share = (p.usage_rate or 0.18) * (p.minutes_proj or 20) * cap
        if p.player_id in foul_trouble:
            share *= 0.55                 # foul trouble: benched earlier
        shares.append(share)
    total_share = sum(shares) or 1.0

    pts = 0
    for _ in range(possessions):
        scored = _sample_possession(ppp, rng)
        if scored:
            pts += scored
            # credit a player by usage share
            roll = rng.random() * total_share
            cum = 0.0
            for (p, cap), share in zip(off_rotation, shares):
                cum += share
                if roll <= cum:
                    sl = stat_lines.setdefault(
                        p.player_id,
                        {"name": p.name, "points": 0,
                         "assists": 0, "rebounds": 0})
                    sl["points"] += scored
                    break
    return pts


def _accumulate_hustle(rotation: list, stat_lines: dict,
                       minutes: float, rng: random.Random):
    """Assists + rebounds per period, scaled from per-game averages."""
    frac = minutes / 48.0
    for p, cap in rotation:
        sl = stat_lines.setdefault(
            p.player_id,
            {"name": p.name, "points": 0, "assists": 0, "rebounds": 0})
        sl["assists"] += max(0, int(rng.gauss((p.apg or 2.0) * frac * cap, 0.7)))
        sl["rebounds"] += max(0, int(rng.gauss((p.rpg or 3.5) * frac * cap, 0.9)))


def simulate_nba_game(context: GameContext,
                      rng: random.Random) -> IterationResult:
    """ONE iteration. Called 10,000 times by the aggregator."""
    home, away = context.home_team, context.away_team

    home_rot = _active_rotation(home, rng)
    away_rot = _active_rotation(away, rng)

    home_score = away_score = 0
    stat_lines: dict = {}
    periods = []
    foul_trouble: set = set()

    # home court edge: small ORtg bump
    home_ortg = (home.team_ortg or config.LEAGUE_AVG_ORTG) + 1.5
    away_ortg = away.team_ortg or config.LEAGUE_AVG_ORTG
    if context.back_to_back_home:
        home_ortg -= 1.8
    if context.back_to_back_away:
        away_ortg -= 1.8

    for q in range(1, 5):
        h = _quarter(home_rot, home_ortg, away.team_drtg,
                     home.team_pace, 12, foul_trouble, stat_lines, rng)
        a = _quarter(away_rot, away_ortg, home.team_drtg,
                     away.team_pace, 12, foul_trouble, stat_lines, rng)
        _accumulate_hustle(home_rot, stat_lines, 12, rng)
        _accumulate_hustle(away_rot, stat_lines, 12, rng)
        home_score += h
        away_score += a
        periods.append({"period": f"Q{q}", "home": h, "away": a})

        # foul trouble check after each quarter (LOCKED decision node)
        for p, cap in home_rot + away_rot:
            if p.is_starter_nba and rng.random() < 0.04:
                foul_trouble.add(p.player_id)

    # Overtime — 5 min periods until winner (garbage time still normal)
    ot = 1
    while home_score == away_score:
        h = _quarter(home_rot, home_ortg, away.team_drtg,
                     home.team_pace, 5, foul_trouble, stat_lines, rng)
        a = _quarter(away_rot, away_ortg, home.team_drtg,
                     away.team_pace, 5, foul_trouble, stat_lines, rng)
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
