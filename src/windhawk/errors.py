"""Compatibility alias for :mod:`windhawk.exceptions`.

The canonical home of the exception hierarchy is :mod:`windhawk.exceptions`.
This module re-exports it so ``from windhawk.errors import ...`` keeps working
for code (and README snippets) written against the shorter name.  New code
should import from :mod:`windhawk` or :mod:`windhawk.exceptions`.
"""

from __future__ import annotations

from .exceptions import (
    CacheError,
    CatalogNotFoundError,
    ModNotFoundError,
    NotFoundError,
    ParseError,
    RateLimitError,
    ResponseTooLargeError,
    ServerError,
    ValidationError,
    VersionNotFoundError,
    WindhawkConnectionError,
    WindhawkError,
    WindhawkHTTPError,
)

__all__ = [
    "CacheError",
    "CatalogNotFoundError",
    "ModNotFoundError",
    "NotFoundError",
    "ParseError",
    "RateLimitError",
    "ResponseTooLargeError",
    "ServerError",
    "ValidationError",
    "VersionNotFoundError",
    "WindhawkConnectionError",
    "WindhawkError",
    "WindhawkHTTPError",
]
