"""
NexGame Lite — AI Read Generator
Kage Software · 2026

Translates a stored SimulationOutput (already-locked, already-graded
prediction) into a short natural-language "read" — the on-camera
talking-point version of the numbers. Built for the daily pick video
workflow: pull today's prediction, generate the read, narrate over it.

Generated ONCE per game and cached (db.save_read / predictions.read_text).
Re-running /api/predict for the same game (e.g. an SP confirmation
update) does NOT wipe the cached read — see db.save_prediction, which
never touches read_text. Call /api/read again explicitly with
?regenerate=true if the prediction changed materially and you want a
fresh read.

This module ONLY narrates numbers that already exist on the locked
prediction. It never invents a stat, a player note, or a probability —
if a field is missing/empty (e.g. no pitching_matchup on an NBA game),
the prompt tells the model to omit that section rather than guess.
"""

import config

try:
    import anthropic
except ImportError:
    anthropic = None   # read feature is optional — rest of the app
                       # runs fine without the package installed


def _build_prompt(pred: dict) -> str:
    """pred: the dict shape returned by db.get_prediction() — raw row
    with player_projections/pitching_matchup already JSON-parsed."""
    sport = pred["sport"]
    home = pred["home_team"]
    away = pred["away_team"]
    home_pct = pred["home_win_pct"]
    away_pct = pred["away_win_pct"]
    win_confidence = pred.get("win_confidence", "toss_up")
    confidence = pred.get("confidence", "medium")

    lines = [
        f"Sport: {sport}",
        f"Matchup: {away} @ {home}",
        f"Model win probability: {home} {home_pct:.1f}% / {away} {away_pct:.1f}%",
        f"Win-margin confidence: {win_confidence}",
        f"Score-range confidence: {confidence}",
        f"Predicted score (median): {home} {pred['score_med_home']} - "
        f"{away} {pred['score_med_away']} "
        f"(range {pred['score_low_home']}-{pred['score_high_home']} / "
        f"{pred['score_low_away']}-{pred['score_high_away']})",
    ]

    pitching = pred.get("pitching_matchup") or {}
    if pitching:
        for side in ("home", "away"):
            p = pitching.get(side)
            if p:
                confirmed = "confirmed" if p.get("confirmed") else "rotation avg — SP not confirmed"
                lines.append(
                    f"{side.title()} pitcher ({confirmed}): {p.get('name', 'n/a')} — "
                    f"ERA {p.get('era')}, WHIP {p.get('whip')}, K/9 {p.get('k_per_9')}"
                )

    projections = pred.get("player_projections") or {}
    if projections:
        top = list(projections.values())[:6]
        lines.append("Key player projections:")
        for p in top:
            stat_bits = ", ".join(
                f"{k} {v}" for k, v in p.items()
                if k not in ("name", "team") and v not in (None, 0, 0.0)
            )
            lines.append(f"  - {p.get('name', 'unknown')} ({p.get('team', '')}): {stat_bits}")

    kalshi_home = pred.get("kalshi_home_prob") or 0.0
    if kalshi_home:
        lines.append(
            f"Market (Kalshi) implied probability: {home} {kalshi_home:.1f}% / "
            f"{away} {pred.get('kalshi_away_prob', 0.0):.1f}%"
        )

    data = "\n".join(lines)

    return (
        "You are writing talking points for a sports betting content creator "
        "who narrates over this exact data on camera every morning. Given the "
        "model output below, write a short read (4-6 sentences) explaining "
        "WHY the model leans the way it does — which specific stat matchup "
        "is driving the edge — not just restating the percentages. "
        "Mention any real uncertainty (e.g. an unconfirmed starter) plainly. "
        "Do not invent any stat, player, or number that isn't in the data "
        "below. Do not state a guaranteed outcome — this is a probabilistic "
        "read, not a lock. Plain sentences, no markdown, no bullet points, "
        "no header.\n\n"
        f"{data}"
    )


def generate_read(pred: dict) -> str:
    """Calls the Anthropic API to generate a read for an already-stored
    prediction. Raises RuntimeError if the API key isn't configured or
    the package isn't installed — callers should catch this and return
    a clear 5xx rather than a confusing stack trace."""
    if anthropic is None:
        raise RuntimeError(
            "anthropic package not installed — add 'anthropic' to "
            "requirements.txt and pip install"
        )
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set — set it in the environment"
        )

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    prompt = _build_prompt(pred)

    response = client.messages.create(
        model=config.READ_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )

    return "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
