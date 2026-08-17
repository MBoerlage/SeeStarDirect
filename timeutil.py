"""Shared time-parsing helper for Alpaca's UTCDate format."""

import re
from datetime import datetime, timezone


def parse_alpaca_datetime(s):
    """Alpaca UTCDate is ISO8601 with variable fractional-second digits,
    e.g. '2026-08-17T16:55:35.1935218Z'. datetime.fromisoformat doesn't
    reliably accept arbitrary fractional-digit counts, so parse by hand.
    Returns a tz-aware UTC datetime, or None if unparseable."""
    if not isinstance(s, str):
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z?$", s)
    if not m:
        return None
    y, mo, d, h, mi, se, frac = m.groups()
    microsecond = int((frac + "000000")[:6]) if frac else 0
    try:
        return datetime(int(y), int(mo), int(d), int(h), int(mi), int(se),
                         microsecond, tzinfo=timezone.utc)
    except ValueError:
        return None
