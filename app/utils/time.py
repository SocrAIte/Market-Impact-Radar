from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dateutil import parser


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return ensure_aware(value)
    try:
        return ensure_aware(parser.parse(value))
    except (TypeError, ValueError, OverflowError):
        return None


def parse_time_window(window: str) -> timedelta:
    normalized = window.strip().lower()
    if normalized.endswith("h"):
        return timedelta(hours=int(normalized[:-1]))
    if normalized.endswith("d"):
        return timedelta(days=int(normalized[:-1]))
    raise ValueError(f"Unsupported time window: {window}. Use 6h, 24h, 3d, or 7d.")


def window_bounds(window: str, until: datetime | None = None) -> tuple[datetime, datetime]:
    end = ensure_aware(until) or utcnow()
    return end - parse_time_window(window), end
