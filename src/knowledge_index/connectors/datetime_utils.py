"""Datetime utilities for consistent timezone handling across the application."""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Get current UTC time - standardized across the application.

    Returns:
        Current datetime in UTC timezone.

    Note:
        This replaces datetime.now() and datetime.utcnow() throughout the codebase
        to ensure consistent timezone handling.
    """
    return datetime.now(timezone.utc)


def utc_now_naive() -> datetime:
    """Get current UTC time as naive datetime for database operations.

    Returns:
        Current datetime in UTC as naive datetime (no timezone info).

    Note:
        This is specifically for SQLAlchemy models that use TIMESTAMP WITHOUT TIME ZONE
        columns. For application logic, use utc_now_naive() instead.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
