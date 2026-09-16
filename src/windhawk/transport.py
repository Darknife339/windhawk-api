"""HTTP transport built on :mod:`urllib.request`.

The library deliberately avoids third-party HTTP clients so it can be dropped
into bots, CLI tools and restricted environments without a dependency tree.
On top of the standard library this module adds what the repository actually
needs:

* conditional requests (``If-None-Match`` / ``304 Not Modified``),
* bounded response bodies (a runaway ``.cpp`` download cannot exhaust memory),
* retries with exponential backoff for transient failures,
* a stable exception mapping (see :mod:`windhawk.exceptions`).
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import io
import json as _json
import random
import re
import socket
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from .exceptions import (
    NotFoundError,
    RateLimitError,
    ResponseTooLargeError,
    ServerError,
    WindhawkConnectionError,
    WindhawkHTTPError,
)

__all__ = ["DEFAULT_TIMEOUT", "AsyncHttpTransport", "HttpResponse", "HttpTransport"]

DEFAULT_TIMEOUT: float = 15.0
DEFAULT_MAX_BYTES: int = 16 * 1024 * 1024
DEFAULT_USER_AGENT: str = "windhawk-api/0.1 (+https://github.com/ramensoftware/windhawk-mods)"

#: Statuses worth retrying: transient backend/proxy failures and rate limits.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

_RETRY_AFTER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")

_CHUNK = 64 * 1024


@dataclass(frozen=True)
class HttpResponse:
    """A minimal, immutable HTTP response."""

    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def etag(self) -> Optional[str]:
        return self.headers.get("etag") or None

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    @property
    def text(self) -> str:
        charset = "utf-8"
        raw = self.headers.get("content-type") or ""
        for part in raw.split(";")[1:]:
            part = part.strip()
            if part.lower().startswith("charset="):
                charset = part.split("=", 1)[1].strip().strip('"') or "utf-8"
                break

        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return _json.loads(self.text or "null")


def _decode_body(raw: bytes, headers: Mapping[str, str]) -> bytes:
    encoding = (headers.get("content-encoding") or "").lower().strip()

    if not raw or not encoding:
        return raw

    try:
        if "gzip" in encoding:
            return gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        if "deflate" in encoding:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error, EOFError):
        # A corrupt body surfaces later as a parse error; returning the raw
        # bytes keeps this helper total (it never raises).
        return raw

    return raw


def _headers_to_dict(info: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        for key in info:
            out[key.lower()] = info.get(key, "")
    except AttributeError:  # pragma: no cover - defensive
        pass
    return out


def _retry_delay(attempt: int, retry_after: Optional[str], backoff: float) -> float:
    if retry_after:
        match = _RETRY_AFTER_RE.match(retry_after)
        if match:
            return min(float(match.group(1)), 30.0)
    return min(backoff * (2**attempt) + random.uniform(0, backoff), 30.0)  # type: ignore[no-any-return]


class HttpTransport:
    """Blocking HTTP GET helper used by the sync client."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 2,
        backoff: float = 0.4,
        max_bytes: int = DEFAULT_MAX_BYTES,
        user_agent: str = DEFAULT_USER_AGENT,
        headers: Optional[Mapping[str, str]] = None,
        opener: Optional[urllib.request.OpenerDirector] = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")

        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff = backoff
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        self.default_headers: Dict[str, str] = dict(headers or {})
        self._opener = opener or urllib.request.build_opener()

    # -- public API ------------------------------------------------------- #
    def get(
        self,
        url: str,
        *,
        etag: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> HttpResponse:
        """GET *url* and return an :class:`HttpResponse`.

        ``304`` comes back as a normal (empty) response rather than an error.
        Error statuses raise :class:`WindhawkHTTPError` subclasses; network
        failures raise :class:`WindhawkConnectionError`.
        """

        request_headers: Dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "*/*",
        }
        request_headers.update(self.default_headers)
        if headers:
            request_headers.update(headers)
        if etag:
            request_headers["If-None-Match"] = etag

        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                return self._perform(url, request_headers, timeout or self.timeout)
            except (RateLimitError, ServerError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise
                delay = _retry_delay(attempt, exc.response_headers.get("retry-after"), self.backoff)
            except WindhawkConnectionError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise
                delay = _retry_delay(attempt, None, self.backoff)

            time.sleep(delay)

        raise last_error or WindhawkConnectionError(f"Request to {url} failed")

    def close(self) -> None:  # pragma: no cover - the opener holds no state
        return None

    # -- internals -------------------------------------------------------- #
    def _perform(self, url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")

        try:
            with self._opener.open(request, timeout=timeout) as response:
                response_headers = _headers_to_dict(response.headers)
                body = self._read(response, response_headers)
                return HttpResponse(
                    url=response.geturl() or url,
                    status=getattr(response, "status", 200),
                    headers=response_headers,
                    body=_decode_body(body, response_headers),
                )
        except urllib.error.HTTPError as exc:
            response_headers = _headers_to_dict(exc.headers)
            try:
                body = self._read(exc, response_headers, limit=min(self.max_bytes, 64 * 1024))
            except (OSError, ResponseTooLargeError):
                body = b""
            finally:
                with contextlib.suppress(Exception):  # pragma: no cover - defensive
                    exc.close()

            return self._handle_http_error(url, exc.code, response_headers, body)
        except socket.timeout as exc:
            raise WindhawkConnectionError(f"Request to {url} timed out after {timeout}s") from exc
        except urllib.error.URLError as exc:
            raise WindhawkConnectionError(f"Cannot reach {url}: {exc.reason}") from exc
        except (ConnectionError, OSError) as exc:
            raise WindhawkConnectionError(f"Cannot reach {url}: {exc}") from exc

    def _read(
        self,
        raw: Any,
        headers: Mapping[str, str],
        *,
        limit: Optional[int] = None,
    ) -> bytes:
        cap = limit or self.max_bytes

        declared = headers.get("content-length")
        if declared:
            try:
                if int(declared) > cap:
                    raise ResponseTooLargeError(
                        f"Response is {declared} bytes, the configured limit is {cap} bytes"
                    )
            except ValueError:
                pass

        chunks: List[bytes] = []
        total = 0

        while True:
            chunk = raw.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise ResponseTooLargeError(
                    f"Response exceeded the {cap} byte limit after {total} bytes"
                )
            chunks.append(chunk)

        return b"".join(chunks)

    def _handle_http_error(
        self,
        url: str,
        status: int,
        headers: Mapping[str, str],
        body: bytes,
    ) -> HttpResponse:
        if status == 304:
            return HttpResponse(url=url, status=304, headers=headers, body=b"")

        snippet = _decode_body(body, headers)[:512].decode("utf-8", errors="replace")

        if status == 404:
            # The client turns this into ModNotFoundError/CatalogNotFoundError.
            raise NotFoundError(
                f"Repository returned HTTP 404 for {url}",
                url=url,
                status_code=404,
                response_body=snippet,
                response_headers=headers,
            )
        if status == 429:
            raise RateLimitError(
                f"Rate limited by {url} (HTTP 429)",
                url=url,
                status_code=status,
                response_body=snippet,
                response_headers=headers,
            )
        if 500 <= status < 600:
            raise ServerError(
                f"Repository returned HTTP {status} for {url}",
                url=url,
                status_code=status,
                response_body=snippet,
                response_headers=headers,
            )

        raise WindhawkHTTPError(
            f"Repository returned HTTP {status} for {url}",
            url=url,
            status_code=status,
            response_body=snippet,
            response_headers=headers,
        )


class AsyncHttpTransport:
    """Async wrapper around :class:`HttpTransport`.

    ``urllib`` is blocking, so requests run on the default executor.  That is
    perfectly adequate for catalog-sized payloads and keeps the library
    dependency-free (no ``aiohttp``/``httpx`` needed).
    """

    def __init__(self, transport: Optional[HttpTransport] = None) -> None:
        self._transport = transport or HttpTransport()

    @property
    def transport(self) -> HttpTransport:
        return self._transport

    async def get(
        self,
        url: str,
        *,
        etag: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> HttpResponse:
        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._transport.get(url, etag=etag, headers=headers, timeout=timeout),
        )

    def close(self) -> None:
        self._transport.close()