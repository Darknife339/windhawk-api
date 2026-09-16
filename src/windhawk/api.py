"""Convenience helpers that do not need a client instance."""

from __future__ import annotations

from typing import Any, List, Optional

from .client import Client
from .models import Catalog, Mod, SearchResult
from .parser import ParsedMod
from .search import SearchFilters

__all__ = ["fetch_catalog", "fetch_mod", "fetch_readme", "fetch_source", "search_mods"]


def fetch_catalog(language: str = "en", **kwargs: Any) -> Catalog:
    """One-shot catalog fetch (creates a throwaway :class:`Client`)."""

    with Client(language=language, **kwargs) as client:
        return client.get_catalog()


def fetch_mod(mod_id: str, **kwargs: Any) -> Mod:
    """One-shot catalog lookup for a single mod."""

    with Client(**kwargs) as client:
        return client.get_mod(mod_id)


def fetch_readme(mod_id: str, **kwargs: Any) -> Optional[str]:
    """One-shot README fetch."""

    with Client(**kwargs) as client:
        return client.get_readme(mod_id)


def fetch_source(mod_id: str, **kwargs: Any) -> ParsedMod:
    """One-shot parse of a mod's ``.wh.cpp`` file."""

    with Client(**kwargs) as client:
        return client.parse_source(mod_id)


def search_mods(
    query: str,
    *,
    limit: int = 20,
    language: str = "en",
    filters: Optional[SearchFilters] = None,
    **kwargs: Any,
) -> List[SearchResult]:
    """One-shot search (fetches the catalog, builds a throwaway index)."""

    with Client(language=language, **kwargs) as client:
        return client.search(query, limit=limit, filters=filters)
