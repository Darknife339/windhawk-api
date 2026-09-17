"""Offline tests: parsing, models, search, cache, client against a fake transport."""

from __future__ import annotations

import json
import urllib.error

import pytest
from conftest import FakeTransport

from windhawk import Client, Mod, ModNotFoundError, parse_mod_source


# --------------------------------------------------------------------------- #
# Models / catalog parsing
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client() -> Client:
    return Client(cache_dir=False, transport=FakeTransport())  # type: ignore[arg-type]


def test_catalog_parse(client: Client) -> None:
    catalog = client.get_catalog()

    assert len(catalog) == 2
    assert catalog.app.version == "1.5.2"
    assert catalog.ids == ("aero-tray", "taskbar-labels")
    assert catalog.total_users == 200000
    assert "aero-tray" in catalog
    assert catalog["aero-tray"].name == "Aero Tray"


def test_mod_fields(client: Client) -> None:
    aero = client.get_mod("aero-tray")

    assert isinstance(aero, Mod)
    assert aero.users == 120000
    # rating comes from ratingBreakdown, not the 0-10 integer
    assert aero.rating == pytest.approx(4.71)
    assert aero.rating_score == 9
    assert aero.rating_users == 200
    assert aero.rating_breakdown == (2, 3, 5, 30, 160)
    assert aero.updated_at is not None and aero.updated_at.year == 2023
    assert aero.published_at is not None and aero.published_at.year == 2020


def test_mod_process_and_architecture(client: Client) -> None:
    aero = client.get_mod("aero-tray")

    assert aero.supports_process("explorer")
    assert aero.supports_process("explorer.exe")
    assert not aero.supports_process("dwm.exe")
    assert aero.supports_architecture("x86-64")
    assert aero.is_compatible(architecture="x86-64", process="explorer")
    assert not aero.is_compatible(process="dwm.exe")


def test_mod_urls(client: Client) -> None:
    aero = client.get_mod("aero-tray")

    assert aero.download_url == "https://mods.windhawk.net/mods/aero-tray.wh.cpp"
    assert aero.versions_api_url == "https://mods.windhawk.net/mods/aero-tray/versions.json"
    assert aero.page_url == "https://windhawk.net/mods/aero-tray"


def test_mod_not_found(client: Client) -> None:
    with pytest.raises(ModNotFoundError) as excinfo:
        client.get_mod("does-not-exist")

    assert excinfo.value.mod_id == "does-not-exist"


def test_catalog_filter_and_sort(client: Client) -> None:
    assert client.list_mods(limit=1)[0].id == "aero-tray"
    assert client.list_mods(process="dwm.exe") == ()
    assert client.list_mods(sort="name")[0].id == "aero-tray"
    assert client.list_mods(min_users=100000)[0].id == "aero-tray"


def test_mod_parse_many_single_mod() -> None:
    # Regression: a bare single-mod object must not be unpacked as a pair.
    single = {"id": "solo-mod", "metadata": {"name": "Solo"}, "details": {"users": 5}}
    mods = Mod.parse_many(single)

    assert len(mods) == 1
    assert mods[0].id == "solo-mod"
    assert mods[0].users == 5


def test_mod_parse_many_malformed_entries_skipped() -> None:
    payload = {
        "mods": {
            "good-mod": {"metadata": {"name": "Good"}, "details": {"users": 1}},
            "": {"metadata": {"name": "Broken"}},
        }
    }
    mods = Mod.parse_many(payload)

    assert [mod.id for mod in mods] == ["good-mod"]


def test_parse_datetime_shapes() -> None:
    from windhawk import parse_datetime

    ms = parse_datetime(1700000000000)
    secs = parse_datetime(1700000000)
    iso = parse_datetime("2023-11-14T22:13:20Z")

    assert ms == secs == iso
    assert ms.tzinfo is not None
    assert parse_datetime(None) is None
    assert parse_datetime("0000-00-00") is None


# --------------------------------------------------------------------------- #
# Source parsing
# --------------------------------------------------------------------------- #
def test_parse_source(client: Client) -> None:
    parsed = client.parse_source("taskbar-labels")

    assert parsed.mod_id == "taskbar-labels"
    assert parsed.version == "2.5"
    assert parsed.author == "ramen software"
    assert parsed.license_note and "GNU General Public License" in parsed.license_note
    assert parsed.readme and parsed.readme.startswith("# Taskbar Labels")
    # README is verbatim: embedded code samples keep their // markers.
    assert "// code sample with // markers" in parsed.readme


def test_parse_settings(client: Client) -> None:
    settings = client.parse_source("taskbar-labels").settings

    assert settings is not None
    assert settings.keys() == ("mode", "spacing")

    mode = settings.get("mode")
    assert mode is not None
    assert mode.default == "labelsWithoutCombining"
    assert mode.name == "Mode"
    assert mode.description == "Choose how labels are shown"
    assert len(mode.options) == 2
    assert mode.options[0].value == "labelsWithoutCombining"
    assert mode.options[0].label == "Labels without combining"

    spacing = settings.get("spacing")
    assert spacing is not None
    assert spacing.default == "8"

    assert settings.defaults() == {"mode": "labelsWithoutCombining", "spacing": "8"}


def test_parse_mod_source_never_raises() -> None:
    empty = parse_mod_source("")
    assert empty.malformed and not empty

    truncated = parse_mod_source("// ==WindhawkMod==\n// @name X\n")
    assert truncated.malformed and truncated.blocks == {}

    closed = parse_mod_source("// ==WindhawkMod==\n// @name X\n// ==/WindhawkMod==\n")
    assert not closed.malformed
    assert closed.metadata.name == "X"


def test_parse_mod_source_crlf() -> None:
    source = "// ==WindhawkMod==\r\n// @name CRLF Mod\r\n// ==/WindhawkMod==\r\n"
    parsed = parse_mod_source(source)

    assert not parsed.malformed
    assert parsed.metadata.name == "CRLF Mod"


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #
def test_versions(client: Client) -> None:
    # Load the catalog first so is_current can be derived from it.
    client.get_catalog()
    versions = client.list_versions("taskbar-labels")

    assert [v.version for v in versions] == ["2.4", "2.5"]
    # versions.json timestamps are epoch seconds
    assert versions[1].published_at is not None and versions[1].published_at.year == 2023
    assert versions[1].is_current


def test_mod_detail_merge(client: Client) -> None:
    detail = client.get_mod_detail("taskbar-labels")

    assert detail.readme is not None
    assert detail.mod.version == "2.5"
    assert len(detail.versions) == 2
    assert any(v.is_current for v in detail.versions)
    assert detail.source is not None and detail.source.from_cache is False

    json.dumps(detail.to_dict())


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def test_search(client: Client) -> None:
    hits = client.search("taskbar labels")

    assert hits and hits[0].mod.id == "taskbar-labels"
    assert hits[0].snippet
    assert "name" in hits[0].matched_fields or "id" in hits[0].matched_fields


def test_search_filters(client: Client) -> None:
    from windhawk import SearchFilters

    hits = client.search("tray", filters=SearchFilters(process="dwm.exe"))
    assert hits == []

    hits = client.search("tray", filters=SearchFilters(min_users=100000))
    assert [h.mod.id for h in hits] == ["aero-tray"]


def test_suggest_and_did_you_mean(client: Client) -> None:
    assert "taskbar" in client.suggest("taskb")
    assert client.did_you_mean("taskbar lables")


def test_search_empty_query(client: Client) -> None:
    hits = client.search("", limit=5)
    assert len(hits) == 2


# --------------------------------------------------------------------------- #
# Validation / security
# --------------------------------------------------------------------------- #
def test_validation_rejects_traversal() -> None:
    from windhawk import ValidationError, validate_mod_id

    for bad in ("../etc/passwd", "..\\windows", "a/b", "http://evil", "", ".hidden", "mod."):
        with pytest.raises(ValidationError):
            validate_mod_id(bad)

    assert validate_mod_id("aero-tray") == "aero-tray"


def test_urls_reject_bad_base() -> None:
    from windhawk import ValidationError
    from windhawk.urls import normalize_base_url

    for bad in ("file:///etc/passwd", "ftp://x", "not a url", "http://"):
        with pytest.raises((ValueError, ValidationError)):
            normalize_base_url(bad)

    assert normalize_base_url("https://mods.windhawk.net/") == "https://mods.windhawk.net"
    assert normalize_base_url(None) == "https://mods.windhawk.net"


def test_client_rejects_bad_base_url() -> None:
    from windhawk import ValidationError

    with pytest.raises(ValidationError):
        Client(base_url="file:///etc/passwd")


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def test_cache_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from windhawk import ResponseCache

    cache = ResponseCache(tmp_path / "cache")

    cache.set("https://x/y", "body", etag='"e1"', ttl=60)
    entry = cache.get("https://x/y")

    assert entry is not None
    assert entry.body == "body"
    assert entry.etag == '"e1"'
    assert not entry.is_expired
    assert entry.validator_headers() == {"If-None-Match": '"e1"'}

    # A fresh instance reads the same entry from disk.
    again = ResponseCache(tmp_path / "cache").get("https://x/y")
    assert again is not None and again.body == "body"


def test_cache_expiry_and_stale(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from windhawk import ResponseCache

    cache = ResponseCache(tmp_path / "cache")
    cache.set("https://x/y", "old", ttl=-1)  # already expired

    assert cache.get("https://x/y") is None
    stale = cache.get("https://x/y", allow_expired=True)
    assert stale is not None and stale.body == "old"


def test_cache_disk_trouble_degrades(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from windhawk import ResponseCache, errors

    cache = ResponseCache(tmp_path / "blocked")

    def boom(*args: object, **kwargs: object) -> None:
        raise errors.CacheError("disk on fire")

    monkeypatch.setattr(cache, "_ensure_directory", boom)

    # Must not raise: disk trouble degrades to "no persistence".
    cache.set("https://x/y", "body")
    assert cache.directory is None


def test_cache_stats_and_clear(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from windhawk import ResponseCache

    cache = ResponseCache(tmp_path / "cache")
    cache.set("https://x/1", "a")
    cache.set("https://x/2", "b")

    stats = cache.stats
    assert stats["disk_entries"] == 2

    assert cache.clear() == 2
    assert cache.stats["disk_entries"] == 0


# --------------------------------------------------------------------------- #
# Transport error mapping
# --------------------------------------------------------------------------- #
def test_transport_maps_404() -> None:
    from windhawk import NotFoundError
    from windhawk.transport import HttpTransport

    class Boom:
        def open(self, *args: object, **kwargs: object) -> None:
            raise urllib.error.HTTPError(
                "https://x/mods/nope.wh.cpp", 404, "Not Found", {}, None  # type: ignore[arg-type]
            )

    transport = HttpTransport(opener=Boom())  # type: ignore[arg-type]

    with pytest.raises(NotFoundError) as excinfo:
        transport.get("https://x/mods/nope.wh.cpp")

    assert excinfo.value.status_code == 404


def test_catalog_fallback_on_missing_language(client: Client) -> None:
    # FakeTransport 404s unknown catalogs; the client must fall back to
    # /catalog.json and still return the (English) catalog.
    catalog = client.get_catalog("xx")

    assert len(catalog) == 2
    assert any(call.endswith("/catalog.json") for call in client.transport.calls)


# --------------------------------------------------------------------------- #
# Public API surface
# --------------------------------------------------------------------------- #
def test_everything_exported_is_importable() -> None:
    import windhawk

    missing = [name for name in windhawk.__all__ if not hasattr(windhawk, name)]

    assert missing == []


def test_one_shot_helpers_are_exported() -> None:
    """README advertises ``windhawk.search_mods(...)``; it must really resolve."""

    import windhawk
    from windhawk import api

    for name in ("fetch_catalog", "fetch_mod", "fetch_readme", "fetch_source", "search_mods"):
        assert name in windhawk.__all__
        assert getattr(windhawk, name) is getattr(api, name)
