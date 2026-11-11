"""TIME fields for the CBU link (docs/protocol.md 4.5) from a zone.

Pure Python, stdlib only. tools/th5link/fakecbu.py imports this file, so the
HA integration and the bench CBU compute the same offset and DST data.
"""
from __future__ import annotations

import datetime as dt

SEARCH_DAYS = 400                        # a year plus slack: every zone changes at most twice
UTC_MAX = 4102444800                     # protocol: next_change_utc must be before 2100


def offset_min(tz: dt.tzinfo, utc: int) -> int:
    """UTC offset of `tz` at epoch second `utc`, in whole minutes."""
    off = dt.datetime.fromtimestamp(utc, tz).utcoffset()
    return int(off.total_seconds()) // 60


def time_fields(tz: dt.tzinfo, utc: int) -> tuple[int, int, int]:
    """(offset_min, next_change_utc, next_offset_min) for the TIME frame.

    next_change_utc is the first second with a different offset, searched
    hourly for SEARCH_DAYS and then bisected. A zone without a change in that
    window (Asia/Kolkata, a fixed offset) gives (offset, 0, 0), which is the
    protocol's "no transition known"."""
    base = offset_min(tz, utc)
    lo = utc
    for hi in range(utc + 3600, utc + SEARCH_DAYS * 86400 + 1, 3600):
        if offset_min(tz, hi) != base:
            while hi - lo > 1:           # offset(lo) == base, offset(hi) != base
                mid = (lo + hi) // 2
                if offset_min(tz, mid) == base:
                    lo = mid
                else:
                    hi = mid
            if hi >= UTC_MAX:
                break
            return base, hi, offset_min(tz, hi)
        lo = hi
    return base, 0, 0
