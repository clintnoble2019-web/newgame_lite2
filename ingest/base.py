"""
NexGame Lite — Data Provider Interface
Kage Software · 2026

THE SWAP PATTERN:
Every provider implements this interface. The rest of the codebase only
ever talks to DataProvider — never to a specific API. Swapping from free
dev APIs to MySportsFeeds at release = one line in config.py.

Fallback chain enforced here (LOCKED):
    Recent Data -> Career Average -> Team Average
    Every player always has a value. The engine never hits a null.
"""

from abc import ABC, abstractmethod
from models import GameContext, PlayerStats, TeamData, Sport


class DataProvider(ABC):
    """Abstract base — all data sources implement this."""

    @abstractmethod
    def get_games_for_date(self, sport: Sport, date_str: str) -> list[GameContext]:
        """Return all games for a sport on a given date (YYYY-MM-DD)."""
        ...

    @abstractmethod
    def get_game_context(self, game_id: str, sport: Sport) -> GameContext:
        """Full game context: teams, rosters, lineups, injuries, fallbacks."""
        ...

    @abstractmethod
    def get_live_scores(self, sport: Sport) -> list[dict]:
        """Live game states for the score ticker. Polled every 60s."""
        ...

    @abstractmethod
    def get_final_boxscore(self, game_id: str, sport: Sport) -> dict:
        """Post-game actuals for the settling pipeline.
        Returns: {'home_score': int, 'away_score': int,
                  'player_stats': {player_id: {metric: value}}}"""
        ...


def get_provider():
    """Factory — reads config.DATA_PROVIDER and returns the right provider.
    THIS is the swap point. Nothing else changes at release."""
    import config
    name = config.DATA_PROVIDER.lower()

    if name == "mock":
        from ingest.mock_provider import MockProvider
        return MockProvider()
    if name == "free":
        from ingest.free_provider import FreeProvider
        return FreeProvider()
    if name == "balldontlie":
        from ingest.balldontlie_provider import BallDontLieProvider
        return BallDontLieProvider()
    if name == "mysportsfeeds":
        from ingest.msf_provider import MySportsFeedsProvider
        return MySportsFeedsProvider()

    raise ValueError(f"Unknown DATA_PROVIDER in config.py: {name}")
