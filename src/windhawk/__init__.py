"""windhawk — typed Python client for the Windhawk mod repository.

Quick start::

    from windhawk import Client

    client = Client()
    for mod in client.top_mods(5):
        print(mod.id, mod.users)

    detail = client.get_mod_detail("aero-tray")
    print(detail.name, detail.readme[:200])

    results = client.search("taskbar labels")

Everything the repository publishes is reachable through :class:`Client`
(catalog, source files, version history) plus a local full-text
:class:`~windhawk.search.ModIndex`, since the repository itself has no search
endpoint.

All errors derive from :class:`~windhawk.exceptions.WindhawkError`.
"""

from __future__ import annotations

from .cache import CacheEntry, ResponseCache, default_cache_dir
from .client import DEFAULT_TTLS, AsyncClient, Client, ModDetail
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
from .models import (
    AppInfo,
    Catalog,
    Mod,
    ModMetadata,
    ModSource,
    SearchResult,
    VersionInfo,
    normalize_mod_id,
    parse_datetime,
    sort_mods,
)
from .parser import (
    ParsedMod,
    SettingEntry,
    SettingOption,
    SettingsBlock,
    extract_blocks,
    parse_mod_source,
    parse_settings_block,
    strip_comment_markers,
)
from .search import ModIndex, SearchFilters, build_index, search_catalog, tokenize
from .transport import HttpResponse, HttpTransport
from .urls import (
    DEFAULT_BASE_URL,
    DEFAULT_GITHUB_SOURCE_URL,
    DEFAULT_LANGUAGE,
    catalog_fallback_url,
    catalog_url,
    github_source_url,
    mod_page_url,
    mod_source_url,
    versions_url,
)
from .validation import sanitize_query, validate_language, validate_mod_id, validate_version

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_GITHUB_SOURCE_URL",
    "DEFAULT_LANGUAGE",
    "DEFAULT_TTLS",
    "AppInfo",
    "AsyncClient",
    "CacheEntry",
    "CacheError",
    "Catalog",
    "CatalogNotFoundError",
    "Client",
    "HttpResponse",
    "HttpTransport",
    "Mod",
    "ModDetail",
    "ModIndex",
    "ModMetadata",
    "ModNotFoundError",
    "ModSource",
    "NotFoundError",
    "ParseError",
    "ParsedMod",
    "RateLimitError",
    "ResponseCache",
    "ResponseTooLargeError",
    "SearchFilters",
    "SearchResult",
    "ServerError",
    "SettingEntry",
    "SettingOption",
    "SettingsBlock",
    "ValidationError",
    "VersionInfo",
    "VersionNotFoundError",
    "WindhawkConnectionError",
    "WindhawkError",
    "WindhawkHTTPError",
    "__version__",
    "build_index",
    "catalog_fallback_url",
    "catalog_url",
    "default_cache_dir",
    "extract_blocks",
    "github_source_url",
    "mod_page_url",
    "mod_source_url",
    "normalize_mod_id",
    "parse_datetime",
    "parse_mod_source",
    "parse_settings_block",
    "sanitize_query",
    "search_catalog",
    "sort_mods",
    "strip_comment_markers",
    "tokenize",
    "validate_language",
    "validate_mod_id",
    "validate_version",
    "versions_url",
]
