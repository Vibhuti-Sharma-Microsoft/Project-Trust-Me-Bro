from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone

_RFC3339 = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$"
)


def timestamp_ns(value: str) -> int:
    match = _RFC3339.fullmatch(value)
    if match is None:
        raise ValueError(f"Expected an explicitly zoned RFC3339 timestamp: {value!r}")
    day, clock, fraction, zone = match.groups()
    base = datetime.strptime(f"{day}T{clock}", "%Y-%m-%dT%H:%M:%S")
    if zone != "Z":
        hours, minutes = map(int, zone[1:].split(":"))
        if hours > 23 or minutes > 59:
            raise ValueError("Invalid timezone offset")
        offset = timedelta(hours=hours, minutes=minutes)
        base = base - offset if zone[0] == "+" else base + offset
    return calendar.timegm(base.timetuple()) * 1_000_000_000 + int((fraction or "").ljust(9, "0"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
