"""Command line interface: ``python -m windhawk <command>``.

Commands:

    search <query>    full-text search over the catalog
    info <mod_id>     catalog entry + README + settings + versions
    catalog           list mods (sort/filter)
    versions <id>     version history
    source <id>       print the raw ``.wh.cpp`` file
    cache <sub>       stats | list | clear

Run ``python -m windhawk <command> --help`` for per-command options.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from . import __version__
from .client import Client
from .exceptions import ValidationError, WindhawkError
from .search import SearchFilters
from .urls import DEFAULT_LANGUAGE

__all__ = ["main"]

_SORT_CHOICES = ("users", "rating", "updated", "published", "name", "author", "id", "sorting")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="windhawk",
        description="Search and inspect Windhawk mods (mods.windhawk.net).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--language", default=DEFAULT_LANGUAGE, help="catalog language (default: en)"
    )
    common.add_argument(
        "--base-url", default=None, help="repository base URL override"
    )
    common.add_argument(
        "--no-cache", action="store_true", help="bypass the response cache"
    )
    common.add_argument(
        "--json", action="store_true", dest="as_json", help="machine readable output"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", parents=[common], help="full-text search")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--sort", default=None, choices=_SORT_CHOICES)
    p_search.add_argument(
        "--process", default=None, help="filter by injected process (e.g. explorer)"
    )
    p_search.add_argument("--author", default=None)
    p_search.add_argument("--min-users", type=int, default=None)
    p_search.add_argument("--min-rating", type=float, default=None)

    p_info = sub.add_parser("info", parents=[common], help="mod details")
    p_info.add_argument("mod_id")
    p_info.add_argument(
        "--readme", action="store_true", help="print the README instead of a summary"
    )
    p_info.add_argument("--settings", action="store_true", help="print parsed settings")
    p_info.add_argument("--source", action="store_true", help="print the raw .wh.cpp source")
    p_info.add_argument(
        "--version", default=None, help="pin a specific version"
    )

    p_catalog = sub.add_parser("catalog", parents=[common], help="list catalog mods")
    p_catalog.add_argument("--limit", type=int, default=20)
    p_catalog.add_argument("--sort", default="users", choices=_SORT_CHOICES)
    p_catalog.add_argument("--process", default=None)
    p_catalog.add_argument("--author", default=None)
    p_catalog.add_argument("--min-users", type=int, default=0)
    p_catalog.add_argument("--min-rating", type=float, default=0.0)

    p_versions = sub.add_parser("versions", parents=[common], help="version history")
    p_versions.add_argument("mod_id")

    p_source = sub.add_parser("source", parents=[common], help="print the raw source file")
    p_source.add_argument("mod_id")
    p_source.add_argument("--version", default=None)

    p_cache = sub.add_parser("cache", parents=[common], help="cache management")
    p_cache.add_argument("action", choices=("stats", "list", "clear"))

    return parser


def _make_client(args: argparse.Namespace) -> Client:
    return Client(
        base_url=args.base_url,
        language=args.language,
        cache_dir=False if args.no_cache else None,
    )


def _print_search(client: Client, args: argparse.Namespace) -> int:
    filters = SearchFilters(
        process=args.process,
        author=args.author,
        min_users=args.min_users,
        min_rating=args.min_rating,
    )
    results = client.search(args.query, limit=args.limit, filters=filters, sort=args.sort)

    if args.as_json:
        print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
        return 0

    if not results:
        suggestions = client.did_you_mean(args.query)
        hint = f"  (did you mean: {', '.join(suggestions)}?)" if suggestions else ""
        print(f"No mods matched {args.query!r}.{hint}")
        return 1

    for result in results:
        stars = f"{result.rating:.1f}" if result.is_rated else "-"
        print(f"{result.id:<40} {result.users:>8} users  {stars:>4}★  {result.name}")
        if result.snippet:
            print(f"    {result.snippet}")
    return 0


def _print_info(client: Client, args: argparse.Namespace) -> int:
    detail = client.get_mod_detail(args.mod_id, version=args.version)

    if args.source and detail.source:
        print(detail.source.content)
        return 0
    if args.readme and detail.readme:
        print(detail.readme)
        return 0
    if args.settings and detail.settings:
        for entry in detail.settings.entries:
            label = f"{entry.name or entry.key} ({entry.key})"
            print(f"- {label} = {entry.default}")
            if entry.description:
                print(f"    {entry.description}")
            for option in entry.options:
                marker = "*" if option.value == entry.default else " "
                print(f"    {marker} {option.value}: {option.label or option.value}")
        return 0

    if args.as_json:
        print(json.dumps(detail.to_dict(), ensure_ascii=False, indent=2))
        return 0

    mod = detail.mod
    print(f"{mod.name}  ({mod.id})")
    if mod.description:
        print(f"  {mod.description}")
    print(f"  author:    {mod.author or '-'}")
    print(f"  version:   {mod.version or '-'}")
    print(f"  users:     {mod.users}")
    print(f"  rating:    {mod.rating:.2f}/5 ({mod.rating_users} ratings)")
    if mod.updated_at:
        print(f"  updated:   {mod.updated_at:%Y-%m-%d}")
    if mod.include:
        print(f"  processes: {', '.join(mod.include)}")
    if mod.license:
        print(f"  license:   {mod.license}")
    print(f"  page:      {mod.page_url}")
    if detail.versions:
        current = next((v.version for v in detail.versions if v.is_current), None)
        print(f"  versions:  {len(detail.versions)} (current: {current or mod.version or '?'})")
    return 0


def _print_catalog(client: Client, args: argparse.Namespace) -> int:
    mods = client.list_mods(
        sort=args.sort,
        process=args.process,
        author=args.author,
        min_users=args.min_users,
        min_rating=args.min_rating,
        limit=args.limit,
    )

    if args.as_json:
        print(json.dumps([m.to_dict() for m in mods], ensure_ascii=False, indent=2))
        return 0

    for mod in mods:
        stars = f"{mod.rating:.1f}" if mod.is_rated else "-"
        print(f"{mod.id:<40} {mod.users:>8} users  {stars:>4}★  {mod.name}")
    return 0


def _print_versions(client: Client, args: argparse.Namespace) -> int:
    versions = client.list_versions(args.mod_id)

    if args.as_json:
        print(json.dumps([v.to_dict() for v in versions], ensure_ascii=False, indent=2))
        return 0

    for info in versions:
        marker = "*" if info.is_current else " "
        when = f"{info.published_at:%Y-%m-%d}" if info.published_at else "?"
        print(f"{marker} {info.version:<16} {when}")
    return 0


def _print_source(client: Client, args: argparse.Namespace) -> int:
    source = client.get_mod_source(args.mod_id, version=args.version)
    print(source.content)
    return 0


def _print_cache(client: Client, args: argparse.Namespace) -> int:
    if args.action == "clear":
        removed = client.cache.clear()
        print(f"Removed {removed} cache entries.")
        return 0

    if args.action == "list":
        for entry in client.cache.entries():
            age = f"{entry.age / 60:.0f}m" if entry.age != float("inf") else "?"
            print(f"{age:>6}  {entry.status}  {entry.url}")
        return 0

    print(json.dumps(client.cache.stats, ensure_ascii=False, indent=2))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    client = _make_client(args)

    handlers = {
        "search": _print_search,
        "info": _print_info,
        "catalog": _print_catalog,
        "versions": _print_versions,
        "source": _print_source,
        "cache": _print_cache,
    }

    try:
        with client:
            return handlers[args.command](client, args)
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except WindhawkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
