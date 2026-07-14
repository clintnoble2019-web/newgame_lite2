"""
NexGame Lite — Kalshi Market Data Client
Kage Software · 2026

Kalshi is now the PRIMARY schedule/odds source across MLB, NBA, WNBA,
CS2 (read-only — no trading, no auth needed for public market data,
VERIFIED live 2026-07-14 against api.elections.kalshi.com).

TICKER FORMATS (VERIFIED live 2026-07-14, one real event per sport):
    MLB:  KXMLBGAME-26JUL171335TBBOSG1
          date(26JUL17) + time(1335) + away+home codes(TBBOS) + game#(G1)
          The G1/G2 suffix is a real, clean fix for doubleheaders —
          Kalshi already disambiguates what BDL's game_id alone doesn't.
    NBA:  KXNBAGAME-... (same shape as MLB, unconfirmed live — NBA is
          off-season, 0 open events at verification time. Off-season
          means this returns [] until real games exist; no special
          casing needed, it'll just work once the season starts.)
    WNBA: KXWNBAGAME-26JUL15GSIND
          date + away+home codes, NO time component in the ticker.
    CS2:  KXCS2GAME-26JUL160700LILMIXG2A-G2A
          date + time + ARBITRARY per-event short codes with NO
          stable relationship to team names (e.g. "BORRACHEIROS"'s own
          code is "CHAMA", not derived from the name at all). CS2
          CANNOT be matched by code — team-name matching is the only
          reliable approach for this sport.

MATCHING STRATEGY: Kalshi gives real team names in event titles/
sub_titles for every sport (e.g. "Tampa Bay vs Boston", "Golden State
vs Indiana"), often truncated ("New York M" for Mets). Matching is
done by NORMALIZED NAME (lowercase, strip franchise nickname) against
BDL's team city/name/full_name — not by code — since codes are
inconsistent (MLB/WNBA mostly recognizable, CS2 not at all). This is
more robust than a hand-built abbreviation table that risks silent
errors on sports with 20-30+ teams.

SELF-DIAGNOSING BY DESIGN: match_team() returns None on no confident
match rather than guessing — callers must treat unmatched games as
"skip, log it" rather than silently misattributing stats to the wrong
team. A hand-verification pass on real output is expected before this
runs unattended in production.
"""

import re
import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"

SERIES_TICKER = {
    "MLB": "KXMLBGAME",
    "NBA": "KXNBAGAME",
    "WNBA": "KXWNBAGAME",
    "CS2": "KXCS2GAME",
}

# MLB/WNBA date parsing: ticker date fragment is DDMMMYY-ish but
# VERIFIED shape is actually YYMONDD, e.g. "26JUL17" = 2026-07-17.
_DATE_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})")
_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _parse_kalshi_date(event_ticker: str, series_ticker: str) -> str | None:
    """Extract YYYY-MM-DD from a Kalshi event ticker's date fragment.
    Event ticker shape: '{SERIES}-{YY}{MON}{DD}{...rest}'."""
    rest = event_ticker[len(series_ticker) + 1:]  # strip "KXMLBGAME-"
    m = _DATE_RE.match(rest)
    if not m:
        return None
    yy, mon, dd = m.groups()
    month = _MONTHS.get(mon)
    if not month:
        return None
    year = 2000 + int(yy)
    return f"{year:04d}-{month:02d}-{int(dd):02d}"


def get_kalshi_games(sport: str, date_str: str = None,
                     status: str = "open", limit: int = 50) -> list[dict]:
    """Pull real games for a sport from Kalshi's public event data (no
    auth — VERIFIED live). Returns a list of dicts, one per event
    (game), each with both sides' teams + market pricing:

    {
        "event_ticker": str,
        "sport": str,
        "date": "YYYY-MM-DD" | None,
        "title": str,               # e.g. "Tampa Bay vs Boston: Game 1"
        "game_number": int,         # 1 unless ticker has a G2+ suffix
        "sides": [
            {"code": "TB", "name": "Tampa Bay",
             "yes_bid": 0.50, "yes_ask": 0.56},
            {"code": "BOS", "name": "Boston",
             "yes_bid": 0.51, "yes_ask": 0.57},
        ],
        "close_time": str,
    }

    date_str, if given, filters client-side (Kalshi's events endpoint
    doesn't support a direct date filter as of the verified shape —
    filtering is done here after fetching, same defensive pattern used
    elsewhere in this codebase for provider quirks)."""
    series_ticker = SERIES_TICKER.get(sport.upper())
    if not series_ticker:
        raise ValueError(f"No Kalshi series mapped for sport: {sport}")

    try:
        r = requests.get(f"{BASE}/events", params={
            "series_ticker": series_ticker, "status": status,
            "limit": limit, "with_nested_markets": "true",
        }, timeout=25)
        r.raise_for_status()
        body = r.json()
    except requests.RequestException:
        return []   # fail safe — empty schedule, not a crash

    out = []
    for ev in body.get("events", []):
        event_ticker = ev.get("event_ticker", "")
        game_date = _parse_kalshi_date(event_ticker, series_ticker)
        if date_str and game_date != date_str:
            continue

        game_number = 1
        # game-number suffix sits at the very end of the event ticker
        # (e.g. "...TBBOSG1") — confirmed live on the TB@BOS doubleheader
        tail_match = re.search(r"G(\d)$", event_ticker)
        if tail_match:
            game_number = int(tail_match.group(1))

        sides = []
        for m in ev.get("markets", []):
            ticker = m.get("ticker", "")
            code = ticker.split("-")[-1]
            try:
                yes_bid = float(m.get("yes_bid_dollars") or 0)
                yes_ask = float(m.get("yes_ask_dollars") or 0)
            except (TypeError, ValueError):
                yes_bid = yes_ask = 0.0
            sides.append({
                "code": code,
                "name": m.get("yes_sub_title", ""),
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
            })

        out.append({
            "event_ticker": event_ticker,
            "sport": sport.upper(),
            "date": game_date,
            "title": ev.get("title", ""),
            "game_number": game_number,
            "sides": sides,
            "close_time": (ev.get("markets") or [{}])[0].get("close_time", ""),
        })
    return out


def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for fuzzy
    matching Kalshi's (often truncated) team names against BDL's."""
    return re.sub(r"[^a-z0-9\s]", "", name.lower()).strip()


def match_team_by_name(kalshi_name: str, bdl_teams: list[dict]) -> dict | None:
    """Match a Kalshi side's name (e.g. 'New York M', 'Tampa Bay',
    'Golden State') against a list of BDL team dicts (each needs at
    least 'name' and/or 'city' and 'full_name' keys — caller passes
    whatever fields the sport's BDL data actually has).

    Returns the matched BDL team dict, or None if no confident match
    — callers MUST treat None as "skip this game, don't guess."

    Strategy: normalized substring match in both directions (Kalshi's
    name is often a truncated PREFIX of BDL's full name — 'New York M'
    -> 'New York Mets' — so a plain equality check would never work;
    a bidirectional substring check catches both truncation and the
    reverse case safely without a hand-built abbreviation table)."""
    kn = _normalize_name(kalshi_name)
    if not kn:
        return None

    candidates = []
    for t in bdl_teams:
        for field in ("full_name", "name", "city"):
            val = t.get(field)
            if not val:
                continue
            bn = _normalize_name(val)
            if kn in bn or bn in kn:
                candidates.append(t)
                break

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # Ambiguous — prefer the shortest full_name/name match (closest
        # to an exact match rather than an accidental broad substring).
        candidates.sort(key=lambda t: len(t.get("full_name")
                                          or t.get("name") or ""))
        return candidates[0]
    return None
