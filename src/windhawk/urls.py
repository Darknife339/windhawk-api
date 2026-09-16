"""URL builders for the Windhawk mod repository.

Every remote path the library knows about lives here.  Two rules make this
module a security boundary rather than a bag of f-strings:

* identifiers must pass :mod:`windhawk.validation` *before* interpolation, so
  ``../`` or ``http://evil/`` payloads can never reach :mod:`urllib`,
* the returned URLs are absolute ``https`` URLs on a host taken from the
  client configuration, never from user input.

Endpoints (see ``windhawk_repository_api.md``):

===============================================  ==========================
``GET /catalogs/{language}.json``                localized catalog
``GET /catalog.json``                            fallback catalog
``GET /mods/{mod_id}.wh.cpp``                    current source
``GET /mods/{mod_id}/{version}.wh.cpp``          pinned version source
``GET /mods/{mod_id}/versions.json``             version history
===============================================  ==========================
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from .validation import validate_language, validate_mod_id, validate_version

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_GITHUB_SOURCE_URL",
    "DEFAULT_LANGUAGE",
    "WINDHAWK_WEBSITE_URL",
    "catalog_fallback_url",
    "catalog_url",
    "github_source_url",
    "mod_page_url",
    "mod_source_url",
    "normalize_base_url",
    "versions_url",
]

#: The repository CDN (Cloudflare/Fastly in front of GitHub).
DEFAULT_BASE_URL: str = "https://mods.windhawk.net"

#: Upstream source of truth for every ``.wh.cpp`` file.
DEFAULT_GITHUB_SOURCE_URL: str = "https://raw.githubusercontent.com/ramensoftware/windhawk-mods/main"

#: Catalog used when a language specific catalog is not published.
DEFAULT_LANGUAGE: str = "en"

WINDHAWK_WEBSITE_URL: str = "https://windhawk.net/"

_MOD_SUFFIX = ".wh.cpp"


def normalize_base_url(base_url: Optional[str] = None) -> str:
    """Validate and canonicalise *base_url* (no trailing slash).

    Only ``http``/``https`` URLs with a host are accepted: this is what stops a
    misconfigured client from turning into ``file:///`` or ``gopher://`` reads.

    Raises:
        ValueError: when the value is not an absolute http(s) URL.
    """

    raw = (base_url or DEFAULT_BASE_URL).strip()

    if not raw:
        raise ValueError("base_url must not be empty")

    parts = urlsplit(raw)

    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError(f"base_url must be an http(s) URL, got {raw!r}")
    if not parts.netloc:
        raise ValueError(f"base_url must include a host, got {raw!r}")

    # Rebuild from the parsed components: gluing the raw string back onto
    # netloc duplicated the host into the path.
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path.rstrip("/"), "", ""))


def catalog_url(language: str = DEFAULT_LANGUAGE, *, base_url: Optional[str] = None) -> str:
    """``/catalogs/{language}.json``."""

    tag = validate_language(language)
    return f"{normalize_base_url(base_url)}/catalogs/{quote(tag, safe='')}.json"


def catalog_fallback_url(*, base_url: Optional[str] = None) -> str:
    """``/catalog.json`` — what Windhawk itself requests on a catalog 404."""

    return f"{normalize_base_url(base_url)}/catalog.json"


def mod_source_url(
    mod_id: str,
    *,
    version: Optional[str] = None,
    base_url: Optional[str] = None,
) -> str:
    """``/mods/{mod_id}.wh.cpp`` or ``/mods/{mod_id}/{version}.wh.cpp``."""

    identifier = validate_mod_id(mod_id)
    base = normalize_base_url(base_url)

    if version is None:
        return f"{base}/mods/{quote(identifier, safe='')}{_MOD_SUFFIX}"

    tag = validate_version(version)
    return f"{base}/mods/{quote(identifier, safe='')}/{quote(tag, safe='')}{_MOD_SUFFIX}"


def versions_url(mod_id: str, *, base_url: Optional[str] = None) -> str:
    """``/mods/{mod_id}/versions.json``."""

    identifier = validate_mod_id(mod_id)
    return f"{normalize_base_url(base_url)}/mods/{quote(identifier, safe='')}/versions.json"


def github_source_url(
    mod_id: str,
    *,
    version: Optional[str] = None,
    base_url: Optional[str] = None,
) -> str:
    """Direct ``raw.githubusercontent.com`` link for the same file.

    Useful as a mirror when the CDN is down, and as a stable permalink when a
    version is pinned (``.../mods/<id>.wh.cpp`` always serves ``main``).
    """

    identifier = validate_mod_id(mod_id)
    base = normalize_base_url(base_url or DEFAULT_GITHUB_SOURCE_URL)

    if version is None:
        return f"{base}/mods/{quote(identifier, safe='')}{_MOD_SUFFIX}"

    tag = validate_version(version)
    return f"{base}/mods/{quote(identifier, safe='')}/{quote(tag, safe='')}{_MOD_SUFFIX}"


def mod_page_url(mod_id: str, *, site_url: str = "https://windhawk.net/mods") -> str:
    """Human facing page on ``windhawk.net``."""

    identifier = validate_mod_id(mod_id)
    return f"{site_url.rstrip('/')}/{quote(identifier, safe='')}"
