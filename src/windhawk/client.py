"""High level client for the Windhawk mod repository.

The repository is a plain static CDN, so the client's job is to make it feel
like an API: fetch once, cache forever-ish, revalidate with ``ETag``, and never
fail just because the CDN hiccuped.

    from windhawk import Client

    client = Client()
    catalog = client.get_catalog()
    mod = client.get_mod("aero-tray")
    readme = client.get_mod_detail("aero-tray").readme

Three behaviours are worth knowing about:

* **stale-if-error** — when a refresh fails but a (possibly expired) cached
  copy exists, that copy is served and :attr:`Catalog.from_cache` is ``True``.
  A script keeps working while the CDN is down.
* **catalog fallback** — a missing ``/catalogs/<lang>.json`` transparently
  retries ``/catalog.json``, which is exactly what Windhawk itself does.
* **lazy search** — :meth:`Client.search` builds a :class:`windhawk.search.ModIndex`
  the first time it is needed and reuses it until the catalog changes.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import threading
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, TypeVar

from .cache import ResponseCache, default_cache_dir
from .exceptions import (
    CatalogNotFoundError,
    ModNotFoundError,
    NotFoundError,
    ParseError,
    ValidationError,
    VersionNotFoundError,
    WindhawkConnectionError,
    WindhawkError,
)
from .models import Catalog, Mod, ModMetadata, ModSource, SearchResult, VersionInfo, sort_mods
from .parser import ParsedMod
from .search import ModIndex, SearchFilters
from .transport import DEFAULT_TIMEOUT, HttpTransport
from .urls import (
    DEFAULT_LANGUAGE,
    catalog_fallback_url,
    catalog_url,
    github_source_url,
    mod_source_url,
    normalize_base_url,
    versions_url,
)
from .validation import validate_language, validate_mod_id, validate_version

__all__ = ["DEFAULT_TTLS", "AsyncClient", "Client", "ModDetail"]

_T = TypeVar("_T")

#: Default cache lifetimes per payload kind (seconds).  The repository is
#: edited by hand, so minutes of staleness are invisible to users but turn a
#: "list 300 mods" loop into a single request.
DEFAULT_TTLS: Mapping[str, Optional[float]] = {
    "catalog": 900.0,
    "source": 3600.0,
    "versions": 1800.0,
}


@dataclass(frozen=True)
class ModDetail:
    """Catalog entry merged with the mod's ``.wh.cpp`` documentation.

    Attributes:
        mod: catalog fields (users, rating, timestamps) enriched with the
            author metadata parsed out of the source file.
        source: the raw ``.wh.cpp`` download.
        parsed: structured view of the source (metadata/readme/settings).
        versions: version history when it was requested.
    """

    mod: Mod
    source: Optional[ModSource] = None
    parsed: Optional[ParsedMod] = None
    versions: Tuple[VersionInfo, ...] = ()

    @property
    def id(self) -> str:
        return self.mod.id

    @property
    def name(self) -> str:
        return self.mod.name

    @property
    def readme(self) -> Optional[str]:
        return self.parsed.readme if self.parsed else None

    @property
    def settings(self):  # type: ignore[no-untyped-def]
        return self.parsed.settings if self.parsed else None

    @property
    def metadata(self) -> ModMetadata:
        return self.mod.metadata

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod": self.mod.to_dict(),
            "readme": self.readme,
            "settings": self.settings.to_dict() if self.settings else None,
            "license_note": self.parsed.license_note if self.parsed else None,
            "versions": [version.to_dict() for version in self.versions],
            "source": (
                {
                    "url": self.source.url,
                    "version": self.source.version,
                    "size": self.source.size,
                    "from_cache": self.source.from_cache,
                }
                if self.source
                else None
            ),
        }


class Client:
    """Blocking client.

    Args:
        base_url: repository root; only ``http``/``https`` hosts are accepted.
        language: catalog language (``en``, ``pt-BR``...).
        cache_dir: where to persist responses; ``None`` uses
            :func:`~windhawk.cache.default_cache_dir`, pass ``False`` to disable
            persistence entirely.
        cache: bring your own :class:`~windhawk.cache.ResponseCache`.
        transport: bring your own :class:`~windhawk.transport.HttpTransport`.
        timeout: per-request timeout in seconds.
        ttl: override :data:`DEFAULT_TTLS` for any subset of payload kinds.
        stale_if_error: serve expired cached data when the network fails.
        fallback_to_default_catalog: retry ``/catalog.json`` when a language
            specific catalog is missing.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        language: str = DEFAULT_LANGUAGE,
        cache_dir: Any = None,
        cache: Optional[ResponseCache] = None,
        transport: Optional[HttpTransport] = None,
        timeout: float = DEFAULT_TIMEOUT,
        ttl: Optional[Mapping[str, Optional[float]]] = None,
        stale_if_error: bool = True,
        fallback_to_default_catalog: bool = True,
        user_agent: Optional[str] = None,
    ) -> None:
        try:
            self.base_url = normalize_base_url(base_url)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        self.language = validate_language(language)
        self.stale_if_error = stale_if_error
        self.fallback_to_default_catalog = fallback_to_default_catalog
        self.ttl: Dict[str, Optional[float]] = dict(DEFAULT_TTLS)
        if ttl:
            self.ttl.update(ttl)

        if cache is not None:
            self.cache = cache
        else:
            enabled = cache_dir is not False
            directory = default_cache_dir() if cache_dir is None else cache_dir
            self.cache = ResponseCache(directory if enabled else None)

        headers = {"User-Agent": user_agent} if user_agent else None
        try:
            self.transport = transport or HttpTransport(timeout=timeout, headers=headers)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        self._index: Optional[ModIndex] = None
        self._index_source: Optional[str] = None
        self._catalog: Optional[Catalog] = None
        self._lock = threading.RLock()

    # -- plumbing --------------------------------------------------------- #
    def _ttl_for(self, kind: str) -> Optional[float]:
        return self.ttl.get(kind)

    def _now(self) -> _dt.datetime:
        return _dt.datetime.now(_dt.timezone.utc)

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Client(base_url={self.base_url!r}, language={self.language!r})"

    # -- core fetch ------------------------------------------------------- #
    def _fetch_text(
        self,
        url: str,
        *,
        kind: str,
        use_cache: bool = True,
        force_refresh: bool = False,
    ) -> Tuple[str, Dict[str, Any]]:
        """Return ``(body, meta)`` for *url*, using the cache when possible.

        ``meta`` carries ``from_cache``, ``etag``, ``last_modified`` and
        ``fetched_at`` so callers can populate their models.
        """

        cached = self.cache.get(url, allow_expired=True) if use_cache else None

        if cached is not None and not force_refresh and not cached.is_expired:
            return cached.body, _meta_from_entry(cached)

        validators = cached.validator_headers() if cached is not None else {}

        try:
            response = self.transport.get(url, headers=validators)
        except WindhawkConnectionError:
            # The CDN is unreachable: an expired copy beats no answer at all.
            if cached is not None and self.stale_if_error:
                return cached.body, _meta_from_entry(cached, stale=True)
            raise

        if response.not_modified and cached is not None:
            refreshed = self.cache.touch(
                cached,
                etag=response.etag,
                last_modified=_last_modified(response.headers),
            )
            return refreshed.body, _meta_from_entry(refreshed)

        meta: Dict[str, Any] = {
            "from_cache": False,
            "etag": response.etag,
            "last_modified": _last_modified(response.headers),
            "fetched_at": self._now(),
            "stale": False,
        }

        if use_cache:
            fetched_at = meta["fetched_at"]
            assert isinstance(fetched_at, _dt.datetime)
            self.cache.set(
                url,
                response.text,
                status=response.status,
                etag=response.etag,
                last_modified=meta["last_modified"],
                content_type=response.headers.get("content-type"),
                ttl=self._ttl_for(kind),
                saved_at=fetched_at.timestamp(),
            )

        return response.text, meta

    def _fetch_json(self, url: str, *, kind: str, **kwargs: Any) -> Any:
        body, meta = self._fetch_text(url, kind=kind, **kwargs)

        try:
            payload = json.loads(body or "null")
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"Repository returned invalid JSON for {url}",
                payload={"error": str(exc), "length": len(body)},
            ) from exc

        return payload, meta

    # -- catalog ---------------------------------------------------------- #
    def get_catalog(
        self,
        language: Optional[str] = None,
        *,
        use_cache: bool = True,
        force_refresh: bool = False,
    ) -> Catalog:
        """Fetch and parse ``/catalogs/<language>.json``.

        Args:
            language: override the client's language for this call.
            use_cache: set ``False`` to bypass the cache entirely.
            force_refresh: revalidate even when the cached copy is fresh.

        Raises:
            CatalogNotFoundError: neither the language catalog nor the
                ``/catalog.json`` fallback exists.
            ParseError: the body is not the expected catalog schema.
        """

        tag = validate_language(language or self.language)
        url = catalog_url(tag, base_url=self.base_url)

        try:
            payload, meta = self._fetch_json(
                url, kind="catalog", use_cache=use_cache, force_refresh=force_refresh
            )
        except NotFoundError:
            # Any 404 (a missing language catalog) falls back to /catalog.json,
            # exactly like the Windhawk client itself does.
            if not self.fallback_to_default_catalog:
                raise

            fallback = catalog_fallback_url(base_url=self.base_url)
            try:
                payload, meta = self._fetch_json(
                    fallback, kind="catalog", use_cache=use_cache, force_refresh=force_refresh
                )
            except CatalogNotFoundError as inner:
                raise CatalogNotFoundError(
                    tag,
                    url=url,
                    response_body=inner.response_body,
                    response_headers=inner.response_headers,
                ) from inner
            url = fallback

        catalog = Catalog.parse(
            payload,
            language=tag,
            source=url,
            fetched_at=meta.get("fetched_at"),
            from_cache=bool(meta.get("from_cache")),
        )

        with self._lock:
            self._catalog = catalog
            if self._index_source != url:
                self._index = None
                self._index_source = url

        return catalog

    @property
    def catalog(self) -> Catalog:
        """The last fetched catalog, fetching it on first access."""

        with self._lock:
            if self._catalog is None:
                return self.get_catalog()
            return self._catalog

    def refresh_catalog(self, language: Optional[str] = None) -> Catalog:
        """Force a network revalidation of the catalog."""

        return self.get_catalog(language, force_refresh=True)

    # -- mods ------------------------------------------------------------- #
    def list_mods(
        self,
        *,
        sort: Optional[str] = "users",
        process: Optional[str] = None,
        author: Optional[str] = None,
        architecture: Optional[str] = None,
        min_users: int = 0,
        min_rating: float = 0.0,
        limit: Optional[int] = None,
    ) -> Tuple[Mod, ...]:
        """Catalog mods, optionally filtered and sorted.

        Raises:
            ValidationError: when *sort* is not a known key (raised by
                :func:`~windhawk.models.sort_mods`).
        """

        catalog = self.catalog.filter(
            process=process,
            author=author,
            architecture=architecture,
            min_users=min_users,
            min_rating=min_rating,
        )

        mods = catalog.sort(sort).mods if sort else catalog.mods
        return mods[:limit] if limit else mods

    def get_mod(self, mod_id: str) -> Mod:
        """Look a single mod up in the catalog.

        Raises:
            ModNotFoundError: the id is not in the catalog.
            ValidationError: the id is malformed (checked before any request).
        """

        identifier = validate_mod_id(mod_id)
        mod = self.catalog.get(identifier)

        if mod is None:
            raise ModNotFoundError(
                identifier, url=catalog_url(self.language, base_url=self.base_url)
            )

        return mod

    def has_mod(self, mod_id: str) -> bool:
        try:
            identifier = validate_mod_id(mod_id)
        except ValidationError:
            return False
        return self.catalog.get(identifier) is not None

    # -- source ----------------------------------------------------------- #
    def get_mod_source(
        self,
        mod_id: str,
        *,
        version: Optional[str] = None,
        use_cache: bool = True,
        force_refresh: bool = False,
        github_fallback: bool = True,
    ) -> ModSource:
        """Download ``/mods/<mod_id>[/<version>].wh.cpp``.

        Args:
            version: pin a specific version instead of ``main``.
            github_fallback: retry on ``raw.githubusercontent.com`` when the CDN
                returns 404 (a freshly merged mod is on GitHub before the CDN).

        Raises:
            ModNotFoundError: neither the CDN nor GitHub has the file.
            VersionNotFoundError: the pinned version does not exist.
        """

        identifier = validate_mod_id(mod_id)
        tag = validate_version(version) if version else None
        url = mod_source_url(identifier, version=tag, base_url=self.base_url)

        try:
            body, meta = self._fetch_text(
                url, kind="source", use_cache=use_cache, force_refresh=force_refresh
            )
        except Exception as exc:
            not_found = _is_not_found(exc)

            if not not_found or not github_fallback:
                raise _translate_source_error(exc, identifier, tag, url) from exc

            mirror = github_source_url(identifier, version=tag)
            try:
                body, meta = self._fetch_text(
                    mirror, kind="source", use_cache=use_cache, force_refresh=force_refresh
                )
                url = mirror
            except Exception as inner:
                raise _translate_source_error(inner, identifier, tag, url) from inner

        return ModSource(
            mod_id=identifier,
            content=body,
            url=url,
            version=tag,
            fetched_at=meta.get("fetched_at"),
            etag=meta.get("etag"),
            last_modified=meta.get("last_modified"),
            size=len(body),
            from_cache=bool(meta.get("from_cache")),
        )

    def parse_source(
        self, mod_id: str, *, version: Optional[str] = None, **kwargs: Any
    ) -> ParsedMod:
        """Download and parse a mod's source in one call."""

        parsed = self.get_mod_source(mod_id, version=version, **kwargs).parsed
        assert isinstance(parsed, ParsedMod)
        return parsed

    # -- versions --------------------------------------------------------- #
    def list_versions(self, mod_id: str, *, use_cache: bool = True) -> Tuple[VersionInfo, ...]:
        """Fetch ``/mods/<mod_id>/versions.json``.

        The current version (the one the catalog advertises) is flagged via
        :attr:`VersionInfo.is_current` when the mod is present in the catalog.
        """

        identifier = validate_mod_id(mod_id)
        url = versions_url(identifier, base_url=self.base_url)

        try:
            payload, _ = self._fetch_json(url, kind="versions", use_cache=use_cache)
        except CatalogNotFoundError as exc:
            raise ModNotFoundError(identifier, url=url, response_body=exc.response_body) from exc

        versions = VersionInfo.parse_many(payload)

        # Flag the current version via the catalog when it is already loaded;
        # self.catalog would trigger a fetch here, which list_versions must
        # not do just to decorate its output.
        known = self._catalog.get(identifier) if self._catalog is not None else None
        current = known.version if known is not None else None

        if current:
            versions = tuple(
                replace(info, is_current=info.version == current) for info in versions
            )

        return versions

    def latest_version(self, mod_id: str) -> Optional[str]:
        """The newest published version string, or ``None`` when unknown."""

        versions = self.list_versions(mod_id)
        if not versions:
            return None
        return max(versions, key=lambda info: info.timestamp or 0).version

    # -- combined --------------------------------------------------------- #
    def get_mod_detail(
        self,
        mod_id: str,
        *,
        version: Optional[str] = None,
        include_versions: bool = True,
        include_source: bool = True,
        use_cache: bool = True,
    ) -> ModDetail:
        """Catalog entry + parsed documentation in one round trip set.

        A missing source never fails the call: the catalog data is still
        useful, so ``source``/``parsed`` simply stay ``None``.
        """

        identifier = validate_mod_id(mod_id)
        mod = self.get_mod(identifier)

        source: Optional[ModSource] = None
        parsed: Optional[ParsedMod] = None
        versions: Tuple[VersionInfo, ...] = ()

        if include_source:
            try:
                source = self.get_mod_source(identifier, version=version, use_cache=use_cache)
            except WindhawkError:
                source = None

            if source is not None:
                parsed = source.parsed
                if parsed and parsed.metadata.mod_id:
                    mod = mod.with_metadata(parsed.metadata)

        if include_versions:
            try:
                versions = self.list_versions(identifier, use_cache=use_cache)
            except WindhawkError:
                versions = ()

        return ModDetail(mod=mod, source=source, parsed=parsed, versions=versions)

    def get_readme(self, mod_id: str) -> Optional[str]:
        """Just the README markdown of a mod."""

        return self.parse_source(mod_id).readme

    # -- search ----------------------------------------------------------- #
    @property
    def index(self) -> ModIndex:
        """Search index over the current catalog (built lazily)."""

        with self._lock:
            if self._index is None:
                self._index = ModIndex(self.catalog)
            return self._index

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: Optional[SearchFilters] = None,
        sort: Optional[str] = None,
    ) -> List[SearchResult]:
        """Full-text search over the catalog (see :class:`windhawk.search.ModIndex`)."""

        return self.index.search(query, limit=limit, filters=filters, sort=sort)

    def suggest(self, prefix: str, *, limit: int = 5) -> List[str]:
        return self.index.suggest(prefix, limit=limit)

    def did_you_mean(self, query: str, *, limit: int = 3) -> List[str]:
        return self.index.did_you_mean(query, limit=limit)

    def top_mods(self, limit: int = 10, *, by: str = "users") -> Tuple[Mod, ...]:
        """The most popular mods — the catalog's own front page."""

        return sort_mods(self.catalog.mods, by=by)[:limit]

    # -- bulk helpers ----------------------------------------------------- #
    def prefetch_sources(self, mod_ids: Optional[List[str]] = None, *, delay: float = 0.0) -> int:
        """Warm the source cache for many mods; returns how many succeeded.

        Failures are swallowed per mod (a removed mod must not abort a crawl).
        ``delay`` throttles the loop so the CDN is not hammered.
        """

        import time

        ids = list(mod_ids) if mod_ids else list(self.catalog.ids)
        fetched = 0

        for identifier in ids:
            try:
                self.get_mod_source(identifier)
                fetched += 1
            except WindhawkError:
                continue
            if delay:
                time.sleep(delay)

        return fetched

    def stats(self) -> Dict[str, Any]:
        """Cache statistics, handy for a ``--stats`` flag."""

        return self.cache.stats


class AsyncClient:
    """Async facade over :class:`Client`.

    The underlying transport is ``urllib`` running on an executor, so the API
    surface mirrors the sync client one-to-one::

        async with AsyncClient() as client:
            catalog = await client.get_catalog()
    """

    def __init__(self, client: Optional[Client] = None, **kwargs: Any) -> None:
        self._client = client or Client(**kwargs)
        self._lock = asyncio.Lock()

    @property
    def client(self) -> Client:
        return self._client

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        # ``async_client.base_url`` and friends stay synchronous attributes.
        try:
            return getattr(self._client, name)
        except AttributeError:
            raise AttributeError(name) from None

    async def __aenter__(self) -> "AsyncClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    async def _run(self, func: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    async def get_catalog(self, language: Optional[str] = None, **kwargs: Any) -> Catalog:
        return await self._run(self._client.get_catalog, language, **kwargs)

    async def get_mod(self, mod_id: str) -> Mod:
        return await self._run(self._client.get_mod, mod_id)

    async def list_mods(self, **kwargs: Any) -> Tuple[Mod, ...]:
        return await self._run(self._client.list_mods, **kwargs)

    async def get_mod_source(self, mod_id: str, **kwargs: Any) -> ModSource:
        return await self._run(self._client.get_mod_source, mod_id, **kwargs)

    async def parse_source(self, mod_id: str, **kwargs: Any) -> ParsedMod:
        return await self._run(self._client.parse_source, mod_id, **kwargs)

    async def list_versions(self, mod_id: str, **kwargs: Any) -> Tuple[VersionInfo, ...]:
        return await self._run(self._client.list_versions, mod_id, **kwargs)

    async def get_mod_detail(self, mod_id: str, **kwargs: Any) -> ModDetail:
        return await self._run(self._client.get_mod_detail, mod_id, **kwargs)

    async def search(self, query: str, **kwargs: Any) -> List[SearchResult]:
        # Indexing is CPU bound but tiny; run it off the loop to stay polite.
        return await self._run(self._client.search, query, **kwargs)

    async def build_index(self) -> ModIndex:
        """Ensure the search index exists (useful at startup)."""

        async with self._lock:
            return await self._run(lambda: self._client.index)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _last_modified(headers: Mapping[str, str]) -> Optional[str]:
    return headers.get("last-modified") or None


def _meta_from_entry(entry: Any, *, stale: bool = False) -> Dict[str, Any]:
    fetched_at = None
    if entry.saved_at:
        fetched_at = _dt.datetime.fromtimestamp(entry.saved_at, tz=_dt.timezone.utc)

    return {
        "from_cache": True,
        "etag": entry.etag,
        "last_modified": entry.last_modified,
        "fetched_at": fetched_at,
        "stale": stale or bool(entry.is_expired),
    }


def _is_not_found(exc: BaseException) -> bool:
    from .exceptions import NotFoundError

    return isinstance(exc, NotFoundError)


def _translate_source_error(
    exc: BaseException, mod_id: str, version: Optional[str], url: str
) -> WindhawkError:
    """Map a transport error onto the mod-specific exception hierarchy."""

    from .exceptions import NotFoundError

    if isinstance(exc, NotFoundError):
        if version:
            return VersionNotFoundError(
                mod_id,
                version,
                url=url,
                response_body=getattr(exc, "response_body", None),
                response_headers=getattr(exc, "response_headers", None),
            )
        return ModNotFoundError(
            mod_id,
            url=url,
            response_body=getattr(exc, "response_body", None),
            response_headers=getattr(exc, "response_headers", None),
        )

    if isinstance(exc, WindhawkError):
        return exc

    return ParseError(f"Cannot read mod source {mod_id!r}: {exc}")
