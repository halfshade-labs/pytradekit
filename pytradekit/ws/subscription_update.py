"""Shared types and helpers for runtime WebSocket subscription updates."""

from dataclasses import dataclass
from typing import FrozenSet, Iterable, Tuple


@dataclass(frozen=True)
class SubscriptionUpdate:
    """Normalized identifiers added to and removed from a subscription."""

    added: Tuple[str, ...]
    removed: Tuple[str, ...]


def normalize_subscription_targets(values: Iterable[str]) -> FrozenSet[str]:
    """Normalize subscription identifiers to non-empty uppercase strings."""
    normalized_values = {
        str(value).strip().upper()
        for value in values
        if str(value).strip()
    }
    return frozenset(normalized_values)


def calculate_subscription_update(
    current: Iterable[str],
    target: Iterable[str],
) -> SubscriptionUpdate:
    """Calculate a deterministic subscription update."""
    current_values = frozenset(current)
    target_values = frozenset(target)
    return SubscriptionUpdate(
        added=tuple(sorted(target_values - current_values)),
        removed=tuple(sorted(current_values - target_values)),
    )
