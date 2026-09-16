"""Exception hierarchy for :mod:`windhawk`.

Every error raised by the library derives from :class:`WindhawkError`, so
callers can guard a whole block with a single ``except`` while still being
able to react to specific failures.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

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

_EMPTY_HEADERS: Mapping[str, str] = {}


class WindhawkError(Exception):
    """Base class for every error raised by this library.

    ``payload`` carries structured context (the offending value, the supported
    alternatives, ...) so callers can render a useful message without parsing
    ``str(exception)``.
    """

    def __init__(self, message: str = "", *, payload: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message)
        self.payload: Dict[str, Any] = dict(payload) if payload else {}


class ValidationError(WindhawkError):
    """A ``mod_id``/``version``/``language`` value is not acceptable.

    Raised *before* any network request is issued.  It is the guard that keeps
    user supplied identifiers from turning into path traversal or SSRF.
    """


class WindhawkHTTPError(WindhawkError):
    """The repository answered with an unexpected HTTP status."""

    def __init__(
        self,
        message: str,
        *,
        url: str,
        status_code: int,
        response_body: Optional[str] = None,
        response_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code
        self.response_body = response_body
        self.response_headers: Mapping[str, str] = response_headers or _EMPTY_HEADERS

    @property
    def retry_after(self) -> Optional[float]:
        """``Retry-After`` in seconds, when the server sent one."""

        raw = self.response_headers.get("retry-after")
        if not raw:
            return None
        try:
            return max(0.0, float(raw.strip()))
        except ValueError:
            return None


class NotFoundError(WindhawkHTTPError):
    """``404`` — the requested resource does not exist in the repository."""


class ModNotFoundError(NotFoundError):
    """The requested mod is not present in the repository."""

    def __init__(
        self,
        mod_id: str,
        *,
        url: str,
        response_body: Optional[str] = None,
        response_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(
            f"Mod {mod_id!r} was not found in the repository",
            url=url,
            status_code=404,
            response_body=response_body,
            response_headers=response_headers,
        )
        self.mod_id = mod_id


class VersionNotFoundError(NotFoundError):
    """The requested mod version is not present in the repository."""

    def __init__(
        self,
        mod_id: str,
        version: str,
        *,
        url: str,
        response_body: Optional[str] = None,
        response_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(
            f"Version {version!r} of mod {mod_id!r} was not found",
            url=url,
            status_code=404,
            response_body=response_body,
            response_headers=response_headers,
        )
        self.mod_id = mod_id
        self.version = version


class CatalogNotFoundError(NotFoundError):
    """No catalog is published for the requested language."""

    def __init__(
        self,
        language: str,
        *,
        url: str,
        response_body: Optional[str] = None,
        response_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(
            f"No catalog is published for language {language!r}",
            url=url,
            status_code=404,
            response_body=response_body,
            response_headers=response_headers,
        )
        self.language = language


class RateLimitError(WindhawkHTTPError):
    """``429`` — the repository asked us to slow down."""


class ServerError(WindhawkHTTPError):
    """``5xx`` — the repository backend failed."""


class WindhawkConnectionError(WindhawkError):
    """Network level failure (DNS, TLS, timeout, refused connection)."""


class ResponseTooLargeError(WindhawkError):
    """The repository returned a body larger than the configured limit.

    Guards against unbounded downloads when a caller passes an unexpected
    ``mod_id`` or the backend starts serving something huge.
    """


class ParseError(WindhawkError):
    """A response did not match the expected repository schema."""


class CacheError(WindhawkError):
    """The on-disk response cache could not be created or written."""
