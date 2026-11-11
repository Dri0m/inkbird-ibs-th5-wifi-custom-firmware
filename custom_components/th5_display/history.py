"""Hourly MCU readings: strict wire decoding and a bounded durable inbox.

No HA imports. Temperatures stay in Celsius centidegrees, humidity in
centipercent, and timestamps are UTC hour starts, independent of DST.
"""
from __future__ import annotations

import base64
import binascii
import re
import struct
from dataclasses import astuple, dataclass

HISTORY_FW = 0x00020003          # first MCU app that sends hourly records
MAX_RECORDS = 8
INBOX_HOURS = 168
RECORD = struct.Struct("<IhhhHHHB")
UTC_MIN = 946684800              # 2000-01-01, as LINK TIME
_SNAPSHOT = re.compile(r"([0-9a-fA-F]{8})/([0-9a-fA-F]{8});(.*)", re.ASCII)


@dataclass(frozen=True, slots=True)
class Hour:
    start: int
    t_mean: int
    t_min: int
    t_max: int
    rh_mean: int
    rh_min: int
    rh_max: int
    samples: int

    def as_list(self) -> list[int]:
        return list(astuple(self))


def _validate(hour: Hour) -> None:
    if hour.start < UTC_MIN or hour.start % 3600:
        raise ValueError("history timestamp is not a UTC hour")
    if not 1 <= hour.samples <= 60:
        raise ValueError("history sample count outside 1..60")
    if not -5500 <= hour.t_min <= hour.t_mean <= hour.t_max <= 14000:
        raise ValueError("history temperature range is invalid")
    if not 0 <= hour.rh_min <= hour.rh_mean <= hour.rh_max <= 10000:
        raise ValueError("history humidity range is invalid")


def parse_history(value: str, now: int) -> list[Hour]:
    """Base64 count/flags/records -> the completed hours, or ValueError; a
    partial or inconsistent payload is refused whole."""
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as err:
        raise ValueError("history is not valid base64") from err
    if len(data) < 2:
        raise ValueError("history header is missing")
    count, flags = data[:2]
    if count > MAX_RECORDS or flags != 0 or len(data) != 2 + count * RECORD.size:
        raise ValueError("history count, flags or length is invalid")
    current = now // 3600 * 3600
    kept: list[Hour] = []
    for i in range(count):
        hour = Hour(*RECORD.unpack_from(data, 2 + i * RECORD.size))
        if hour.start >= current:
            continue  # stamped by a clock that ran ahead: drop it, keep the rest
        _validate(hour)
        if kept and hour.start <= kept[-1].start:
            raise ValueError("history hours are not strictly increasing")
        kept.append(hour)
    return kept


def parse_snapshot(value: str, now: int, *, nonce: str, boot: int | None) -> list[Hour]:
    """`nonce/boot;payload` -> hours, only for this exact session.

    ESPHome may publish a retained History state before the new Session, and
    the nonce can repeat across CBU cold boots, so both must match before
    anything is stored or acknowledged."""
    if (match := _SNAPSHOT.fullmatch(value)) is None:
        raise ValueError("history session envelope is missing or malformed")
    if boot is None or match[1].casefold() != nonce.casefold() or int(match[2], 16) != boot:
        raise ValueError("history belongs to a different session")
    return parse_history(match[3], now)


def load_inbox(rows: list[list[int]]) -> list[Hour]:
    """The stored inbox (history.json, written by this integration). Rows that
    aren't 8 ints are skipped, so a hand-edited file can't fail setup."""
    return merge_inbox([], [Hour(*row) for row in rows if isinstance(row, list)
                            and len(row) == 8 and all(type(v) is int for v in row)])


def merge_inbox(previous: list[Hour], received: list[Hour]) -> list[Hour]:
    """Latest record wins for a UTC hour; keep the INBOX_HOURS most recent."""
    by_hour = {h.start: h for h in (*previous, *received)}
    return [by_hour[start] for start in sorted(by_hour)[-INBOX_HOURS:]]
