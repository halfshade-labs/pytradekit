"""Sequence-aware Binance Spot order book synchronization."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, Mapping, Sequence, Tuple


PriceLevel = Tuple[Decimal, Decimal]


class BinanceDepthSequenceGap(RuntimeError):
    """Raised when a Binance depth event is not contiguous with the local book."""

    def __init__(
        self,
        expected_update_id: int,
        first_update_id: int,
        final_update_id: int,
    ) -> None:
        self.expected_update_id = expected_update_id
        self.first_update_id = first_update_id
        self.final_update_id = final_update_id
        super().__init__(
            "Binance depth sequence gap: "
            f"expected U={expected_update_id}, received U={first_update_id}, u={final_update_id}"
        )


@dataclass(frozen=True)
class BinanceDepthCheckpoint:
    """A bounded, immutable view of the synchronized order book."""

    last_update_id: int
    synced: bool
    bids: Tuple[PriceLevel, ...]
    asks: Tuple[PriceLevel, ...]


class BinanceSpotDepthSync:
    """Apply Binance Spot diff-depth events to a REST depth snapshot."""

    def __init__(self) -> None:
        self._bids: Dict[Decimal, Decimal] = {}
        self._asks: Dict[Decimal, Decimal] = {}
        self._last_update_id = 0
        self._snapshot_loaded = False
        self._synced = False

    @property
    def last_update_id(self) -> int:
        self._require_snapshot()
        return self._last_update_id

    @property
    def synced(self) -> bool:
        return self._synced

    @property
    def best_bid(self) -> PriceLevel:
        self._require_snapshot()
        if not self._bids:
            raise LookupError("Binance bid book is empty")
        price = max(self._bids)
        return price, self._bids[price]

    @property
    def best_ask(self) -> PriceLevel:
        self._require_snapshot()
        if not self._asks:
            raise LookupError("Binance ask book is empty")
        price = min(self._asks)
        return price, self._asks[price]

    def load_snapshot(self, snapshot: Mapping[str, object]) -> None:
        """Replace local state with a Binance REST depth snapshot."""
        last_update_id = self._parse_update_id(snapshot, "lastUpdateId")
        bids = self._parse_levels(snapshot, "bids")
        asks = self._parse_levels(snapshot, "asks")
        self._bids = self._build_side(bids)
        self._asks = self._build_side(asks)
        self._last_update_id = last_update_id
        self._snapshot_loaded = True
        self._synced = False

    def apply_event(self, event: Mapping[str, object]) -> bool:
        """Apply one diff-depth event, returning False when it is stale."""
        self._require_snapshot()
        first_update_id = self._parse_update_id(event, "U")
        final_update_id = self._parse_update_id(event, "u")
        self._validate_event_range(first_update_id, final_update_id)
        if final_update_id <= self._last_update_id:
            return False
        self._validate_sequence(first_update_id, final_update_id)
        bids = self._parse_levels(event, "b")
        asks = self._parse_levels(event, "a")
        self._apply_side(self._bids, bids)
        self._apply_side(self._asks, asks)
        self._last_update_id = final_update_id
        self._synced = True
        return True

    def checkpoint(self, levels: int = 20) -> BinanceDepthCheckpoint:
        """Return the best bounded levels without exposing mutable book state."""
        self._require_snapshot()
        if levels <= 0:
            raise ValueError("checkpoint levels must be positive")
        bids = tuple(sorted(self._bids.items(), reverse=True)[:levels])
        asks = tuple(sorted(self._asks.items())[:levels])
        return BinanceDepthCheckpoint(self._last_update_id, self._synced, bids, asks)

    def _validate_sequence(self, first_update_id: int, final_update_id: int) -> None:
        expected_update_id = self._last_update_id + 1
        if first_update_id <= expected_update_id <= final_update_id:
            return
        raise BinanceDepthSequenceGap(
            expected_update_id, first_update_id, final_update_id
        )

    @staticmethod
    def _validate_event_range(first_update_id: int, final_update_id: int) -> None:
        if first_update_id > final_update_id:
            raise ValueError(
                f"Binance depth event has invalid range: U={first_update_id}, u={final_update_id}"
            )

    @staticmethod
    def _parse_update_id(payload: Mapping[str, object], key: str) -> int:
        try:
            update_id = int(payload[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Binance depth payload has invalid {key}") from exc
        if update_id < 0:
            raise ValueError(f"Binance depth payload has negative {key}: {update_id}")
        return update_id

    @classmethod
    def _parse_levels(
        cls, payload: Mapping[str, object], key: str
    ) -> Tuple[PriceLevel, ...]:
        raw_levels = payload.get(key)
        if not isinstance(raw_levels, (list, tuple)):
            raise ValueError(f"Binance depth payload has invalid {key} levels")
        return tuple(cls._parse_level(level, key) for level in raw_levels)

    @staticmethod
    def _parse_level(raw_level: object, key: str) -> PriceLevel:
        if not isinstance(raw_level, (list, tuple)) or len(raw_level) < 2:
            raise ValueError(f"Binance depth payload has invalid {key} price level")
        try:
            price = Decimal(str(raw_level[0]))
            quantity = Decimal(str(raw_level[1]))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(
                f"Binance depth payload has non-decimal {key} price level"
            ) from exc
        if not price.is_finite() or price <= 0:
            raise ValueError(f"Binance depth payload has invalid {key} price: {price}")
        if not quantity.is_finite() or quantity < 0:
            raise ValueError(
                f"Binance depth payload has invalid {key} quantity: {quantity}"
            )
        return price, quantity

    @staticmethod
    def _build_side(levels: Sequence[PriceLevel]) -> Dict[Decimal, Decimal]:
        return {price: quantity for price, quantity in levels if quantity != 0}

    @staticmethod
    def _apply_side(book: Dict[Decimal, Decimal], levels: Sequence[PriceLevel]) -> None:
        for price, quantity in levels:
            if quantity == 0:
                book.pop(price, None)
            else:
                book[price] = quantity

    def _require_snapshot(self) -> None:
        if not self._snapshot_loaded:
            raise RuntimeError("Binance depth snapshot has not been loaded")
