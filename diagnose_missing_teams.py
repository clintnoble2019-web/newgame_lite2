"""
Diagnostic — checks why specific teams' games (Athletics/MLB, Portland/
WNBA reported missing 2026-07-19) aren't appearing in the schedule.

get_games_for_date() is Kalshi-gated for MLB/NBA/WNBA (see its own
docstring in ingest/balldontlie_provider.py): a game only shows if it
exists as a real Kalshi market AND that market's team names/codes
successfully cross-match to a real BDL game shell. A game can go
missing for two structurally different reasons that look identical
from the dashboard:
    (A) Kalshi has no market for this game at all today — a coverage
        gap on Kalshi's side, not a bug in this codebase.
    (B) Kalshi HAS a market, but match_team_by_name() (see
        ingest/kalshi_client.py) fails to link it to the BDL shell —
        a real matching bug. This exact class of bug already hit once
        for Toronto Tempo (new WNBA expansion team, BDL returned just
        "Tempo" with no city) — Portland is a same-season expansion
        team, so this is the leading suspect for that one. Athletics
        recently dropped "Oakland" from its official name, which is a
        different but adjacent kind of naming-format change.

This script calls the REAL functions this app actually uses
(get_kalshi_games, BallDontLieProvider._bdl_shells_for_date,
_match_kalshi_to_shell) rather than reimplementing the logic, so
whatever it prints reflects exactly what the live app is doing.

Run: python diagnose_missing_teams.py
"""
import sys
sys.path.insert(0, ".")
import config
from datetime import date, timedelta

from models import Sport
from ingest.kalshi_client import get_kalshi_games, match_team_by_name, _normalize_name
from ingest.balldontlie_provider import BallDontLieProvider, _match_kalshi_to_shell

provider = BallDontLieProvider()

TARGETS = [
    (Sport.MLB, "athletics"),
    (Sport.WNBA, "portland"),
]

for sport, needle in TARGETS:
    print(f"\n{'=' * 70}")
    print(f"  {sport.value.upper()} — looking for '{needle}'")
    print("=" * 70)

    for offset in (0, 1):   # today and tomorrow — catches either date
        d = (date.today() + timedelta(days=offset)).isoformat()
        print(f"\n--- date: {d} ---")

        try:
            bdl_shells = provider._bdl_shells_for_date(sport, d)
        except Exception as e:
            print(f"  BDL shell fetch FAILED: {e}")
            continue
        print(f"  BDL shells found: {len(bdl_shells)}")
        target_shells = [
            s for s in bdl_shells
            if needle in _normalize_name(s.home_team.name)
            or needle in _normalize_name(s.away_team.name)
        ]
        if target_shells:
            for s in target_shells:
                print(f"    MATCH in BDL: {s.away_team.name} @ {s.home_team.name}"
                     f"  (home.abbrev={getattr(s.home_team, 'abbrev', '?')!r},"
                     f" away.abbrev={getattr(s.away_team, 'abbrev', '?')!r})")
        else:
            print(f"    No BDL shell contains '{needle}' in home/away team.name "
                 f"— team names present: "
                 f"{sorted(set(s.home_team.name for s in bdl_shells) | set(s.away_team.name for s in bdl_shells))}")

        try:
            kalshi_games = get_kalshi_games(sport.value, d)
        except Exception as e:
            print(f"  Kalshi fetch FAILED: {e}")
            continue
        print(f"  Kalshi markets found: {len(kalshi_games)}")
        target_kalshi = [
            kg for kg in kalshi_games
            if any(needle in _normalize_name(s.get("name", "")) for s in kg.get("sides", []))
        ]
        if target_kalshi:
            for kg in target_kalshi:
                sides_desc = ", ".join(
                    f"{s.get('name','?')!r} (code={s.get('code','?')!r})"
                    for s in kg.get("sides", []))
                print(f"    MATCH in Kalshi: {kg.get('title','?')} — sides: {sides_desc}")
        else:
            all_side_names = sorted(set(
                s.get("name", "") for kg in kalshi_games for s in kg.get("sides", [])))
            print(f"    No Kalshi market contains '{needle}' in any side name "
                 f"— side names present today: {all_side_names}")

        # If both a BDL shell AND a Kalshi market exist independently,
        # the real question is whether _match_kalshi_to_shell actually
        # links them — run it for real and show the verdict.
        if target_shells and target_kalshi:
            print("  Both sources have this team independently — testing "
                 "the ACTUAL cross-match function now:")
            for kg in target_kalshi:
                shell, home_side, away_side = _match_kalshi_to_shell(kg, bdl_shells)
                if shell:
                    print(f"    -> MATCHED: {kg.get('title')} -> "
                         f"{shell.away_team.name} @ {shell.home_team.name}")
                else:
                    print(f"    -> NO MATCH — this is the bug. Kalshi sides "
                         f"{[s.get('name') for s in kg.get('sides', [])]} did not "
                         f"cross-match any BDL shell's home_team.name/away_team.name "
                         f"or abbreviation code.")
        elif target_shells and not target_kalshi:
            print("  DIAGNOSIS: BDL has this game, Kalshi does NOT have a "
                 "market for it today — this is case (A), a Kalshi coverage "
                 "gap, not a code bug. The game is correctly excluded per "
                 "get_games_for_date()'s Kalshi-gating design for this sport.")
        elif target_kalshi and not target_shells:
            print("  DIAGNOSIS: Kalshi has a market, BDL does NOT return a "
                 "game shell for this team on this date — check the date "
                 "(timezone offset?) or whether BDL has this game scheduled "
                 "at all right now.")
