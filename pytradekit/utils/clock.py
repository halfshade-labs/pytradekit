"""Lightweight high-resolution wall and monotonic clocks."""

import time


def get_timestamp_us() -> int:
    """Return the UTC Unix wall-clock timestamp in microseconds."""
    return time.time_ns() // 1_000


def get_timestamp_ns() -> int:
    """Return the UTC Unix wall-clock timestamp in nanoseconds."""
    return time.time_ns()


def get_monotonic_timestamp_ns() -> int:
    """Return a monotonic nanosecond clock for local elapsed-time measurement."""
    return time.monotonic_ns()
