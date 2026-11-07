"""Canonical UTC timestamp serialization."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.types import String, TypeDecorator


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_after(seconds: int | float, base: str | None = None) -> str:
    start = datetime.fromisoformat((base or utc_now()).replace("Z", "+00:00"))
    return (
        datetime.fromtimestamp(start.timestamp() + seconds, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class UTCDateTime(TypeDecorator[str]):
    """Store timezone-aware values as canonical, lexicographically sortable UTC text."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: datetime | str | None, dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            parsed = value
        if parsed.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def process_result_value(self, value: str | None, dialect) -> datetime | None:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
