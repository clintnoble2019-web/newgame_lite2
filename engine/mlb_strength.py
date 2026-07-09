"""
NexGame Lite — MLB Team Strength (Four-Factor Intersection Engine)
Kage Software · 2026

Ported directly from Samurai Picks (Gambling Degen), Clint's live MLB
betting engine — the same architecture that's been paper/live-traded
and tuned across dozens of sessions. Adapted here to feed NexGame's
per-plate-appearance simulation instead of a single closed-form win %,
but the philosophy is identical:

    Every team's scoring strength is the product of FOUR EXPLICIT,
    DECOUPLED factors, each centered at 1.0 and individually clamped
    so none of them can saturate and swallow the others:

        team_strength = HIT × oppPITCH × oppDEF × SCORE

    HIT      — this team's own batting quality (their OBP/SLG vs league)
    oppPITCH — the opposing pitcher's weakness (>1 = bad arm, they get hit)
    oppDEF   — the opposing defense's weakness (neutral until real
               fielding data is wired in — matches the source model's
               "neutral on missing data" guarantee, never fabricates edge)
    SCORE    — recent scoring pace (neutral until rolling run totals are
               tracked — same missing-data guarantee)

    An all-league-average matchup returns team_strength = 1.0 for both
    sides, so two fallback-average teams no longer look identical purely
    by coincidence — any real difference in team_obp/team_slg/rotation_era
    that DOES exist in the data now actually separates them.

Blowout pin (LOCKED, ported exactly): a starter at or above BLOWOUT_ERA
gets pinned to BLOWOUT_WEAKNESS rather than let the ratio math run wild —
this is the same fix that solved the "Athletics 98.9% win prob" bug in
the source model.
"""

import config

# ── League baselines (already exist in config.py, referenced here) ──
LEAGUE_AVG_OBP  = config.LEAGUE_AVG_OBP
LEAGUE_AVG_SLG  = 0.410
LEAGUE_AVG_ERA  = config.LEAGUE_AVG_ERA
LEAGUE_AVG_WHIP = config.LEAGUE_AVG_WHIP

# ── Ported constants (LOCKED, same values as Samurai Picks) ──────────
BLOWOUT_ERA      = 7.50
BLOWOUT_WEAKNESS = 1.33   # 1 / 0.75 — inverse of the old prevention floor

# ── Clamps — each factor individually bounded so none saturates ─────
HIT_CLAMP   = (0.85, 1.15)
PITCH_CLAMP = (0.75, 1.25)
DEF_CLAMP   = (0.94, 1.06)   # narrow — DEF is a gentle lever, not a driver

# Exponents controlling how sharply each ratio bites (ported)
ERA_EXP  = 1.0
WHIP_EXP = 0.6


# Dampening factor — NexGame's per-PA math already uses batter.obp and
# pitcher.whip directly, so the team-level four-factor multiplier would
# otherwise apply the SAME signal twice (team_obp overlaps individual
# batter.obp under fallback conditions especially) and compound scoring
# too aggressively. Dampen the DEVIATION from 1.0 rather than apply the
# raw multiplier — same "trim, not throttle" principle as the source
# model's style overlay, which caps its effect rather than letting it
# fully re-drive the base math.
STRENGTH_DAMPEN = 0.28


def clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def build_hit_composite(team_obp: float, team_slg: float) -> float:
    """
    FACTOR 1 — HIT (this team's own batting quality).
        > 1.0 = above-average bats -> they score more
        < 1.0 = below-average bats -> they score less
    Geometric-mean blend of OBP ratio and SLG ratio (ported approach:
    two independent signals combined multiplicatively, then clamped).
    """
    obp_ratio = max(team_obp, 0.200) / LEAGUE_AVG_OBP
    slg_ratio = max(team_slg, 0.250) / LEAGUE_AVG_SLG
    raw = (obp_ratio * slg_ratio) ** 0.5
    return clamp(raw, HIT_CLAMP[0], HIT_CLAMP[1])


def build_pitch_weakness(era: float, whip: float) -> tuple[float, bool]:
    """
    FACTOR 2 — oppPITCH (the opposing pitcher's weakness).
        > 1.0 = weak pitching -> the OTHER team scores more off them
        < 1.0 = strong pitching -> the OTHER team scores less
    Blowout pin (LOCKED, ported exactly): an ERA at/above BLOWOUT_ERA
    pins straight to BLOWOUT_WEAKNESS instead of letting the ratio
    explode — this is the fix for the source model's "Athletics 98.9%"
    bug, ported verbatim rather than re-derived.
    Returns (weakness, is_blowout).
    """
    if era >= BLOWOUT_ERA:
        return clamp(BLOWOUT_WEAKNESS, PITCH_CLAMP[0], PITCH_CLAMP[1]), True

    era_component  = max(era, 0.50) / LEAGUE_AVG_ERA
    whip_component = max(whip, 0.50) / LEAGUE_AVG_WHIP
    raw = (era_component ** ERA_EXP) * (whip_component ** WHIP_EXP)
    return clamp(raw, PITCH_CLAMP[0], PITCH_CLAMP[1]), False


def build_def_weakness(team_der: float = None) -> float:
    """
    FACTOR 3 — oppDEF (the opposing defense's weakness).
        > 1.0 = below-average glove -> the OTHER team scores a touch more
        < 1.0 = above-average glove -> the OTHER team scores a touch less
    NexGame doesn't track fielding/DER data yet (BallDontLie GOAT tier
    doesn't expose it at the endpoints currently wired). Ported
    guarantee preserved: neutral (1.0) when data is missing rather
    than fabricating an edge from nothing. Hook is here for when
    fielding data becomes available.
    """
    if team_der is None:
        return 1.0
    der = max(team_der, 0.600)
    league_avg_der = 0.700
    raw = 1.0 + ((league_avg_der / der) - 1.0) * 1.50   # DEF_AMP, ported
    return clamp(raw, DEF_CLAMP[0], DEF_CLAMP[1])


def build_score_factor(recent_runs_per_game: float = None) -> float:
    """
    FACTOR 4 — SCORE (recent scoring pace, lightly weighted).
    NexGame doesn't track a rolling last-N-games run total yet.
    Neutral (1.0) when missing, same guarantee as above. Hook is here
    for when rolling game logs are wired in.
    """
    if recent_runs_per_game is None:
        return 1.0
    league_avg_rpg = 4.30
    ratio = max(recent_runs_per_game, 1.0) / league_avg_rpg
    # Ported weighting: SCORE is lightly weighted (0.30) relative to
    # the other three factors, so it nudges rather than dominates.
    dampened = 1.0 + (ratio - 1.0) * 0.30
    return clamp(dampened, 0.90, 1.10)


def team_strength(team, opp_pitcher_era: float,
                  opp_pitcher_whip: float) -> dict:
    """
    THE INTERSECTION — combines all four factors for one team's
    at-bat strength against a specific opposing pitcher.

    team_strength_multiplier = HIT × oppPITCH × oppDEF × SCORE

    Returns a dict (not just the final number) so the multiplier is
    traceable back to each factor — same transparency principle as
    the source model's "Edge src:" decomposition line.
    """
    hit = build_hit_composite(team.team_obp or LEAGUE_AVG_OBP,
                              team.team_slg or LEAGUE_AVG_SLG)
    pitch_weak, is_blowout = build_pitch_weakness(opp_pitcher_era,
                                                   opp_pitcher_whip)
    def_weak = build_def_weakness()      # neutral — no fielding data yet
    score = build_score_factor()          # neutral — no rolling logs yet

    raw_multiplier = hit * pitch_weak * def_weak * score
    # Dampen the deviation from 1.0 — see STRENGTH_DAMPEN comment above.
    multiplier = 1.0 + (raw_multiplier - 1.0) * STRENGTH_DAMPEN

    return {
        "hit": round(hit, 3),
        "opp_pitch_weakness": round(pitch_weak, 3),
        "opp_def_weakness": round(def_weak, 3),
        "score_factor": round(score, 3),
        "multiplier": round(multiplier, 3),
        "is_blowout": is_blowout,
    }
