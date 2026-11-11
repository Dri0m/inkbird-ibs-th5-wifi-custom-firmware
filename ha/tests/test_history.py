"""Strict history tail validation and durable-inbox merging without HA."""
from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components"))
from th5_display import history  # noqa: E402

NOW = int(dt.datetime(2026, 11, 2, 12, tzinfo=dt.UTC).timestamp())
HOUR = history.Hour(NOW - 3600, -123, -456, 123, 5000, 4500, 6000, 60)


def tail(hours: list[history.Hour], flags: int = 0) -> str:
    return base64.b64encode(bytes((len(hours), flags)) + b"".join(
        history.RECORD.pack(*h.as_list()) for h in hours)).decode()


def test_parse_signed_centidegrees_and_empty_tail():
    assert history.parse_history(tail([HOUR]), NOW) == [HOUR]
    assert history.parse_history(tail([]), NOW) == []
    hours = [dataclasses.replace(HOUR, start=HOUR.start - i * 3600) for i in reversed(range(8))]
    value = tail(hours)
    assert len(value) == 184
    assert history.parse_history(value, NOW) == hours


@pytest.mark.parametrize("value", ["", "unknown", "!abc", "AA==", "AA==junk", "x" * 185,
                                  tail([HOUR], flags=1), tail([HOUR])[:-4],
                                  base64.b64encode(b"\x09\x00").decode()])
def test_reject_malformed_tail(value):
    with pytest.raises(ValueError):
        history.parse_history(value, NOW)


def test_future_hour_is_dropped_not_fatal():
    future = dataclasses.replace(HOUR, start=NOW // 3600 * 3600)
    assert history.parse_history(tail([HOUR, future]), NOW) == [HOUR]


@pytest.mark.parametrize("change", [{"start": NOW - 3599}, {"start": 0},
                                    {"samples": 0}, {"samples": 61}, {"t_mean": 124},
                                    {"t_min": -6000}, {"rh_mean": 6001}, {"rh_max": 10001}])
def test_reject_invalid_rows(change):
    with pytest.raises(ValueError):
        history.parse_history(tail([dataclasses.replace(HOUR, **change)]), NOW)


def test_reject_duplicates_and_unsorted_tail():
    earlier = dataclasses.replace(HOUR, start=HOUR.start - 3600)
    for rows in ([HOUR, HOUR], [HOUR, earlier]):
        with pytest.raises(ValueError, match="increasing"):
            history.parse_history(tail(rows), NOW)


def test_inbox_replaces_duplicates_and_bounds_storage():
    hours = [dataclasses.replace(HOUR, start=HOUR.start - i * 3600) for i in reversed(range(200))]
    updated = dataclasses.replace(HOUR, t_mean=0)
    merged = history.merge_inbox(hours, [updated])
    assert len(merged) == 168 and merged[-1] == updated
    assert merged == sorted(merged, key=lambda h: h.start)
    assert history.load_inbox([h.as_list() for h in reversed(merged)]) == merged


def test_utc_hours_survive_dst_fall_back_without_colliding():
    first = int(dt.datetime(2026, 10, 25, 0, tzinfo=dt.UTC).timestamp())
    hours = [dataclasses.replace(HOUR, start=first + i * 3600) for i in range(3)]
    assert history.parse_history(tail(hours), NOW) == hours
    assert len(history.merge_inbox([], hours)) == 3


def test_snapshot_requires_matching_nonce_and_boot():
    payload = tail([HOUR])
    assert history.parse_snapshot(f"7D150E72/00000042;{payload}", NOW,
                                  nonce="7d150e72", boot=0x42) == [HOUR]
    assert history.parse_snapshot("7d150e72/00000042;AAA=", NOW,
                                  nonce="7d150e72", boot=0x42) == []
    for nonce, boot in (("7d150e73", 0x42), ("7d150e72", 0x43), ("7d150e72", None)):
        with pytest.raises(ValueError, match="different session"):
            history.parse_snapshot(f"7d150e72/00000042;{payload}", NOW, nonce=nonce, boot=boot)


@pytest.mark.parametrize("prefix", ["", "7d150e72;", "7d150e72/42;", "7d150e72/gggggggg;",
                                    "7d150e72;00000042;", "7d150e72/00000042;extra;"])
def test_snapshot_rejects_unbound_or_malformed_envelope(prefix):
    with pytest.raises(ValueError):
        history.parse_snapshot(prefix + tail([HOUR]), NOW, nonce="7d150e72", boot=0x42)


def test_max_snapshot_stays_under_ha_state_limit():
    hours = [dataclasses.replace(HOUR, start=HOUR.start - i * 3600) for i in reversed(range(8))]
    value = f"7d150e72/00000042;{tail(hours)}"
    assert len(value) == 202
    assert history.parse_snapshot(value, NOW, nonce="7d150e72", boot=0x42) == hours


def test_load_inbox_skips_rows_that_are_not_eight_ints():
    rows = [HOUR.as_list(), ["a"] * 8, [1, 2], None, [*HOUR.as_list()[:7], 1.5]]
    assert history.load_inbox(rows) == [HOUR]
