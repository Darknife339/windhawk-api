"""Thread-safe HTTP cache with ETag revalidation and TTL expiry.

Two layers:

* an in-process LRU (cheap, dies with the process),
* an on-disk JSON store under ``~/.cache/windhawk-api`` (survives restarts and
  is what makes repeated ``get_catalog()`` calls free).

Entries keep the server's ``ETag``/``Last-Modified`` so the client can send
``If-None-Match``/``If-Modified-Since`` and turn a refresh into a 304 with no
body.  When the network fails the cache can also serve *stale* content
(``stale_if_error``), which is what keeps a script working while Windhawk's CDN
is having a bad minute.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from .errors import CacheError

__all__ = ["CacheEntry", "ResponseCache", "default_cache_dir"]

_DEFAULT_MAX_MEMORY_ENTRIES = 128


def default_cache_dir() -> Path:
    """Cache directory, overridable with ``WINDHAWK_CACHE_DIR``.

    ``XDG_CACHE_HOME`` is honoured on every platform; on Windows the per-user
    ``LOCALAPPDATA`` folder is used when XDG is not configured.
    """

    override = os.environ.get("WINDHAWK_CACHE_DIR")
    if override:
        return Path(override).expanduser()

    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "windhawk-api"

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return Path(base).expanduser() / "windhawk-api" / "cache"

    return Path.home() / ".cache" / "windhawk-api"


@dataclass(frozen=True)
class CacheEntry:
    """A cached response.

    Attributes:
        url: cache key source (the absolute request URL).
        body: decoded response text.
        status: status code that produced this body.
        saved_at: unix timestamp of when the entry was written.
        ttl: lifetime in seconds (``None`` = only revalidated by ETag).
        etag / last_modified: validators forwarded on the next request.
        content_type: ``content-type`` of the original response.
    """

    url: str
    body: str
    status: int = 200
    saved_at: float = 0.0
    ttl: Optional[float] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    content_type: Optional[str] = None

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.saved_at) if self.saved_at else float("inf")

    @property
    def is_expired(self) -> bool:
        return self.ttl is not None and self.age > self.ttl

    def validator_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        elif self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        return headers

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "CacheEntry":
        payload = json.loads(text)
        known = {
            "url",
            "body",
            "status",
            "saved_at",
            "ttl",
            "etag",
            "last_modified",
            "content_type",
        }
        return cls(**{key: value for key, value in payload.items() if key in known})


class ResponseCache:
    """LRU + disk cache for text responses.

    Args:
        directory: where to persist entries; ``None`` disables persistence.
        ttl: default lifetime in seconds for entries written without one.
        max_memory_entries: size of the in-process LRU.
        enabled: set to ``False`` for a no-op cache (useful in tests).
    """

    def __init__(
        self,
        directory: Optional["os.PathLike[str]"] = None,
        *,
        ttl: Optional[float] = 900.0,
        max_memory_entries: int = _DEFAULT_MAX_MEMORY_ENTRIES,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.ttl = ttl
        self.directory = Path(directory).expanduser() if directory is not None else None
        self._memory: "OrderedDict[str, CacheEntry]" = OrderedDict()
        self._max_memory = max(1, int(max_memory_entries))
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._revalidations = 0
        self._disk_ready = self.directory is None

    # -- keys ------------------------------------------------------------- #
    @staticmethod
    def key_for(url: str) -> str:
        """Stable, filesystem-safe key for *url*."""

        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]

    def _path_for(self, url: str) -> Optional[Path]:
        if self.directory is None:
            return None
        return self.directory / f"{self.key_for(url)}.json"

    def _ensure_directory(self) -> None:
        if self._disk_ready or self.directory is None:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._disk_ready = True
        except OSError as exc:
            raise CacheError(
                f"Cannot create the cache directory {self.directory}",
                payload={"error": str(exc)},
            ) from exc

    # -- read/write ------------------------------------------------------- #
    def get(self, url: str, *, allow_expired: bool = False) -> Optional[CacheEntry]:
        """Return the cached entry for *url*, or ``None``.

        Args:
            allow_expired: also return entries past their TTL.  The client
                uses this to revalidate with an ``If-None-Match`` request and,
                on network failure, to serve stale data.
        """

        if not self.enabled:
            return None

        with self._lock:
            entry = self._memory.get(url)

            if entry is None:
                entry = self._read_disk(url)

            if entry is None:
                self._misses += 1
                return None

            if entry.is_expired and not allow_expired:
                # Keep it around for stale_if_error, but report a miss.
                self._misses += 1
                return None

            self._hits += 1
            self._remember(url, entry)
            return entry

    def set(
        self,
        url: str,
        body: str,
        *,
        status: int = 200,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        content_type: Optional[str] = None,
        ttl: Optional[float] = ...,  # type: ignore[assignment]
        saved_at: Optional[float] = None,
    ) -> CacheEntry:
        """Store *body* under *url* and return the created entry."""

        effective_ttl: Optional[float] = self.ttl if ttl is ... else ttl  # type: ignore[comparison-overlap]

        entry = CacheEntry(
            url=url,
            body=body,
            status=status,
            saved_at=saved_at if saved_at is not None else time.time(),
            ttl=effective_ttl,
            etag=etag,
            last_modified=last_modified,
            content_type=content_type,
        )

        if not self.enabled:
            return entry

        with self._lock:
            self._remember(url, entry)
            self._write_disk(entry)

        return entry

    def touch(self, entry: CacheEntry, **updates: Any) -> CacheEntry:
        """Refresh an existing entry's timestamp/validators (304 handling)."""

        payload = asdict(entry)
        payload.update({key: value for key, value in updates.items() if value is not None})
        payload["saved_at"] = updates.get("saved_at", time.time())
        refreshed = CacheEntry(**payload)

        with self._lock:
            self._remember(refreshed.url, refreshed)
            self._write_disk(refreshed)
            self._revalidations += 1

        return refreshed

    def invalidate(self, url: str) -> bool:
        """Drop the entry for *url*.  Returns whether anything was removed."""

        with self._lock:
            existed = self._memory.pop(url, None) is not None

            path = self._path_for(url)
            if path is not None and path.exists():
                try:
                    path.unlink()
                    existed = True
                except OSError:
                    pass

            return existed

    def clear(self) -> int:
        """Remove every entry.  Returns how many files were deleted."""

        removed = 0

        with self._lock:
            self._memory.clear()

            if self.directory is None or not self.directory.exists():
                return removed

            for path in self.directory.glob("*.json"):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue

        return removed

    # -- introspection ---------------------------------------------------- #
    @property
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "memory_entries": len(self._memory),
                "disk_entries": self.disk_entry_count(),
                "hits": self._hits,
                "misses": self._misses,
                "revalidations": self._revalidations,
                "directory": str(self.directory) if self.directory else None,
            }

    def disk_entry_count(self) -> int:
        if self.directory is None or not self.directory.exists():
            return 0
        try:
            return sum(1 for _ in self.directory.glob("*.json"))
        except OSError:
            return 0

    @property
    def size_bytes(self) -> int:
        if self.directory is None or not self.directory.exists():
            return 0
        total = 0
        try:
            for path in self.directory.glob("*.json"):
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        except OSError:
            return 0
        return total

    def entries(self) -> Iterator[CacheEntry]:
        """Yield every on-disk entry (used by the CLI's ``cache list``)."""

        if self.directory is None or not self.directory.exists():
            return

        for path in sorted(self.directory.glob("*.json")):
            entry = self._load_file(path)
            if entry is not None:
                yield entry

    def urls(self) -> Tuple[str, ...]:
        return tuple(entry.url for entry in self.entries())

    # -- internals -------------------------------------------------------- #
    def _remember(self, url: str, entry: CacheEntry) -> None:
        self._memory[url] = entry
        self._memory.move_to_end(url)
        while len(self._memory) > self._max_memory:
            self._memory.popitem(last=False)

    def _read_disk(self, url: str) -> Optional[CacheEntry]:
        path = self._path_for(url)
        if path is None:
            return None
        entry = self._load_file(path)
        if entry is not None:
            self._memory[url] = entry
        return entry

    @staticmethod
    def _load_file(path: Path) -> Optional[CacheEntry]:
        try:
            return CacheEntry.from_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # A truncated or foreign file must never crash the library; the
            # next successful response simply overwrites it.
            with contextlib.suppress(OSError):
                path.unlink()
            return None

    def _write_disk(self, entry: CacheEntry) -> None:
        if self.directory is None:
            return

        try:
            self._ensure_directory()
            path = self._path_for(entry.url)
            if path is None:
                return

            payload = entry.to_json()

            # Write to a temp file in the same directory, then rename: a
            # concurrent reader never observes a half-written entry.
            handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                os.replace(temp_name, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temp_name)
                raise
        except (OSError, CacheError) as exc:
            # Disk trouble degrades to "no persistence", never to a failure.
            # ``CacheError`` is included because ``_ensure_directory`` raises
            # it when the directory cannot be created.
            self.directory = None
            self._disk_ready = True
            self._write_failure = exc

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ResponseCache(directory={str(self.directory) if self.directory else None!r}, "
            f"ttl={self.ttl}, enabled={self.enabled})"
        )