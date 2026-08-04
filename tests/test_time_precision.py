from unittest.mock import patch

from pytradekit.utils.clock import (
    get_monotonic_timestamp_ns,
    get_timestamp_ns,
    get_timestamp_us,
)
from pytradekit.utils.time_handler import (
    get_monotonic_timestamp_ns as get_monotonic_timestamp_ns_from_time_handler,
)
from pytradekit.utils.time_handler import (
    get_timestamp_ns as get_timestamp_ns_from_time_handler,
)
from pytradekit.utils.time_handler import (
    get_timestamp_us as get_timestamp_us_from_time_handler,
)


def test_high_resolution_wall_clocks_share_one_source() -> None:
    with patch("pytradekit.utils.clock.time.time_ns", return_value=1_234_567_890):
        assert get_timestamp_ns() == 1_234_567_890
        assert get_timestamp_us() == 1_234_567


def test_monotonic_clock_is_explicitly_separate() -> None:
    with patch("pytradekit.utils.clock.time.monotonic_ns", return_value=987_654_321):
        assert get_monotonic_timestamp_ns() == 987_654_321


def test_high_resolution_clocks_are_reexported_from_time_handler() -> None:
    assert get_timestamp_us_from_time_handler is get_timestamp_us
    assert get_timestamp_ns_from_time_handler is get_timestamp_ns
    assert get_monotonic_timestamp_ns_from_time_handler is get_monotonic_timestamp_ns
