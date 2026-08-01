"""Datetime utilities for consistent timezone handling across the application."""

from datetime import datetime, timezone


def utc_now_naive() -> datetime:
    """Get current UTC time as naive datetime for database operations.

    Returns:
        Current datetime in UTC as naive datetime (no timezone info).

    Note:
        This is specifically for SQLAlchemy models that use TIMESTAMP WITHOUT TIME ZONE
        columns.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
