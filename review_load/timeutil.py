"""Small time helpers (Python 3.9 compatible)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse a GitHub timestamp such as 2026-09-04T15:20:00Z into an aware datetime."""
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    ts = datetime.fromisoformat(value)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def hours_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600.0


def iso(ts: Optional[datetime]) -> Optional[str]:
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_hours(hours: Optional[float]) -> str:
    """38 min, 5.2 h, 3.1 d."""
    if hours is None:
        return "n/a"
    if hours < 1:
        return f"{max(0, round(hours * 60))} min"
    if hours < 48:
        return f"{hours:.1f} h"
    return f"{hours / 24:.1f} d"
