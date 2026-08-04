from decimal import Decimal

import pytest

from pytradekit.ws.binance_depth_sync import (
    BinanceDepthSequenceGap,
    BinanceSpotDepthSync,
)


def make_snapshot():
    return {
        "lastUpdateId": 100,
        "bids": [["100.0", "2.0"], ["99.0", "3.0"]],
        "asks": [["101.0", "4.0"], ["102.0", "5.0"]],
    }


def make_event(first_update_id, final_update_id, bids=None, asks=None):
    return {
        "U": first_update_id,
        "u": final_update_id,
        "b": bids or [],
        "a": asks or [],
    }


def test_first_event_bridges_snapshot_next_update_id():
    depth_sync = BinanceSpotDepthSync()
    depth_sync.load_snapshot(make_snapshot())

    applied = depth_sync.apply_event(make_event(99, 101))

    assert applied is True
    assert depth_sync.synced is True
    assert depth_sync.last_update_id == 101


def test_stale_event_is_discarded_without_marking_book_synced():
    depth_sync = BinanceSpotDepthSync()
    depth_sync.load_snapshot(make_snapshot())

    applied = depth_sync.apply_event(make_event(98, 100, bids=[["100.0", "9.0"]]))

    assert applied is False
    assert depth_sync.synced is False
    assert depth_sync.last_update_id == 100
    assert depth_sync.best_bid == (Decimal("100.0"), Decimal("2.0"))


def test_first_event_must_cover_snapshot_next_update_id():
    depth_sync = BinanceSpotDepthSync()
    depth_sync.load_snapshot(make_snapshot())

    with pytest.raises(BinanceDepthSequenceGap) as exc_info:
        depth_sync.apply_event(make_event(102, 103))

    assert exc_info.value.expected_update_id == 101
    assert exc_info.value.first_update_id == 102
    assert exc_info.value.final_update_id == 103


def test_subsequent_event_rejects_first_update_id_above_expected():
    depth_sync = BinanceSpotDepthSync()
    depth_sync.load_snapshot(make_snapshot())
    depth_sync.apply_event(make_event(101, 102))

    with pytest.raises(BinanceDepthSequenceGap, match=r"expected U=103"):
        depth_sync.apply_event(make_event(104, 105))


def test_subsequent_overlapping_event_is_safe_to_apply():
    depth_sync = BinanceSpotDepthSync()
    depth_sync.load_snapshot(make_snapshot())
    depth_sync.apply_event(make_event(101, 102))

    applied = depth_sync.apply_event(make_event(102, 104, bids=[["100.0", "8.0"]]))

    assert applied is True
    assert depth_sync.last_update_id == 104
    assert depth_sync.best_bid == (Decimal("100.0"), Decimal("8.0"))


def test_updates_deletes_and_checkpoints_decimal_levels():
    depth_sync = BinanceSpotDepthSync()
    depth_sync.load_snapshot(make_snapshot())

    depth_sync.apply_event(
        make_event(
            101,
            101,
            bids=[["100.0", "7.5"], ["99.0", "0"], ["98.0", "1.25"]],
            asks=[["101.0", "0"], ["103.0", "6.5"]],
        )
    )

    checkpoint = depth_sync.checkpoint(levels=10)
    assert depth_sync.best_bid == (Decimal("100.0"), Decimal("7.5"))
    assert depth_sync.best_ask == (Decimal("102.0"), Decimal("5.0"))
    assert checkpoint.last_update_id == 101
    assert checkpoint.synced is True
    assert checkpoint.bids == (
        (Decimal("100.0"), Decimal("7.5")),
        (Decimal("98.0"), Decimal("1.25")),
    )
    assert checkpoint.asks == (
        (Decimal("102.0"), Decimal("5.0")),
        (Decimal("103.0"), Decimal("6.5")),
    )
