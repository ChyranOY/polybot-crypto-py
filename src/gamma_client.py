"""
Gamma API Client - Market Discovery for Polymarket

Provides access to the Gamma API for discovering active markets,
including 15-minute Up/Down markets for crypto assets.

Example:
    from src.gamma_client import GammaClient

    client = GammaClient()
    market = client.get_current_market("ETH",interval="15m")
    print(market["slug"], market["clobTokenIds"])
"""

import json
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from .http import ThreadLocalSessionMixin


class GammaClient(ThreadLocalSessionMixin):
    """
    Client for Polymarket's Gamma API.

    Used to discover markets and get market metadata.
    """

    DEFAULT_HOST = "https://gamma-api.polymarket.com"

    SUPPORTED_COINS = {"BTC", "ETH", "SOL", "XRP"}
    INTERVAL_SECONDS = {
        "5m": 300,
        "15m": 900,
        "1h": 3600,
    }

    def __init__(self, host: str = DEFAULT_HOST, timeout: int = 10):
        """
        Initialize Gamma client.

        Args:
            host: Gamma API host URL
            timeout: Request timeout in seconds
        """
        super().__init__()
        self.host = host.rstrip("/")
        self.timeout = timeout

    def get_market_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """
        Get market data by slug.

        Args:
            slug: Market slug (e.g., "eth-updown-15m-1766671200")

        Returns:
            Market data dictionary or None if not found
        """
        url = f"{self.host}/markets/slug/{slug}"

        try:
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception:
            return None

    def _validate_coin(self, coin: str) -> str:
        coin = coin.upper()
        if coin not in self.SUPPORTED_COINS:
            raise ValueError(f"Unsupported coin: {coin}. Use: {list(self.SUPPORTED_COINS)}")
        return coin

    def _validate_interval(self, interval: str) -> str:
        interval = interval.lower()
        if interval not in self.INTERVAL_SECONDS:
            raise ValueError(
                f"Unsupported interval: {interval}. Use: {list(self.INTERVAL_SECONDS.keys())}"
            )
        return interval

    def _slug_prefix(self, coin: str, interval: str) -> str:
        return f"{coin.lower()}-updown-{interval}"

    def _current_window_ts(self, interval_seconds: int) -> int:
        now = datetime.now(timezone.utc)
        return int(now.timestamp() // interval_seconds * interval_seconds)

    def get_current_market(self, coin: str, interval: str = "15m") -> Optional[Dict[str, Any]]:
        coin = self._validate_coin(coin)
        interval = self._validate_interval(interval)
        interval_seconds = self.INTERVAL_SECONDS[interval]
        prefix = self._slug_prefix(coin, interval)

        current_ts = self._current_window_ts(interval_seconds)
        # We need to pick the market whose end date is in the future.
        for ts in (current_ts, current_ts + interval_seconds, current_ts + 2 * interval_seconds, current_ts - interval_seconds):
            slug = f"{prefix}-{ts}"
            market = self.get_market_by_slug(slug)
            
            if market and market.get("active") and not market.get("closed") and market.get("acceptingOrders"):
                end_date_str = market.get("endDate")
                if end_date_str:
                    try:
                        end_time = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        # We must guarantee the market has not ended yet.
                        if end_time > now:
                            return market
                    except Exception:
                        return market # fallback
                else:
                    return market

        return None

    def get_next_market(self, coin: str, interval: str = "15m") -> Optional[Dict[str, Any]]:
        coin = self._validate_coin(coin)
        interval = self._validate_interval(interval)
        interval_seconds = self.INTERVAL_SECONDS[interval]
        prefix = self._slug_prefix(coin, interval)

        current_ts = self._current_window_ts(interval_seconds)
        next_ts = current_ts + interval_seconds
        slug = f"{prefix}-{next_ts}"
        return self.get_market_by_slug(slug)

    def parse_token_ids(self, market: Dict[str, Any]) -> Dict[str, str]:
        """
        Parse token IDs from market data.

        Args:
            market: Market data dictionary

        Returns:
            Dictionary with "up" and "down" token IDs
        """
        clob_token_ids = market.get("clobTokenIds", "[]")
        token_ids = self._parse_json_field(clob_token_ids)

        outcomes = market.get("outcomes", '["Up", "Down"]')
        outcomes = self._parse_json_field(outcomes)

        return self._map_outcomes(outcomes, token_ids)

    def parse_prices(self, market: Dict[str, Any]) -> Dict[str, float]:
        """
        Parse current prices from market data.

        Args:
            market: Market data dictionary

        Returns:
            Dictionary with "up" and "down" prices
        """
        outcome_prices = market.get("outcomePrices", '["0.5", "0.5"]')
        prices = self._parse_json_field(outcome_prices)

        outcomes = market.get("outcomes", '["Up", "Down"]')
        outcomes = self._parse_json_field(outcomes)

        return self._map_outcomes(outcomes, prices, cast=float)

    @staticmethod
    def _parse_json_field(value: Any) -> List[Any]:
        """Parse a field that may be a JSON string or a list."""
        if isinstance(value, str):
            return json.loads(value)
        return value

    @staticmethod
    def _map_outcomes(
        outcomes: List[Any],
        values: List[Any],
        cast=lambda v: v
    ) -> Dict[str, Any]:
        """Map outcome labels to values with optional casting."""
        result: Dict[str, Any] = {}
        for i, outcome in enumerate(outcomes):
            if i < len(values):
                result[str(outcome).lower()] = cast(values[i])
        return result

    def get_market_info(self, coin: str, interval: str = "15m") -> Optional[Dict[str, Any]]:
        """
        Get comprehensive market info for current 15-minute market.

        Args:
            coin: Coin symbol

        Returns:
            Dictionary with market info including token IDs and prices
        """
        market = self.get_current_market(coin, interval=interval)
        if not market:
            return None

        token_ids = self.parse_token_ids(market)
        prices = self.parse_prices(market)

        return {
            "slug": market.get("slug"),
            "question": market.get("question"),
            "end_date": market.get("endDate"),
            "token_ids": token_ids,
            "prices": prices,
            "accepting_orders": market.get("acceptingOrders", False),
            "best_bid": market.get("bestBid"),
            "best_ask": market.get("bestAsk"),
            "spread": market.get("spread"),
            "raw": market,
        }
