"""Offline smoke test: a fake transport serves canned repository payloads."""

from __future__ import annotations

import json
import sys
import urllib.error

sys.path.insert(0, "src")

from windhawk import Client, Mod, ModNotFoundError, parse_mod_source
from windhawk.transport import HttpResponse

CATALOG = {
    "app": {"version": "1.5.2", "versionBleedingEdge": "1.6.0"},
    "mods": {
        "aero-tray": {
            "metadata": {
                "name": "Aero Tray",
                "description": "Windows 11 tray with Windows 10 look",
                "author": "m417z",
                "version": "2.0",
                "include": "explorer.exe",
                "architecture": ["x86", "x86-64"],
                "donateUrl": "https://example.org/donate",
            },
            "details": {
                "users": 120000,
                "rating": 9,
                "ratingUsers": 200,
                "ratingBreakdown": [2, 3, 5, 30, 160],
                "published": 1600000000000,
                "updated": 1700000000000,
                "defaultSorting": 500,
            },
        },
        "taskbar-labels": {
            "metadata": {
                "name": "Taskbar Labels for Windows 11",
                "description": "Adds labels next to taskbar buttons",
                "author": "ramen software",
                "version": "2.5",
                "include": "explorer.exe",
            },
            "details": {
                "users": 80000,
                "rating": 8,
                "ratingUsers": 100,
                "ratingBreakdown": [1, 1, 8, 20, 70],
                "published": 1610000000000,
                "updated": 1690000000000,
            },
        },
    },
}

SOURCE = """// Wh.cpp
// ==WindhawkMod==
// @id              taskbar-labels
// @name            Taskbar Labels for Windows 11
// @description     Adds labels next to taskbar buttons
// @version         2.5
// @author          ramen software
// @github          https://github.com/ramensoftware
// @include         explorer.exe
// @architecture    x86-64
// @compilerOptions -lcomctl32
// ==/WindhawkMod==

// Source code is published under The GNU General Public License v3.0.

#include <windows.h>

// ==WindhawkModReadme==
/*
# Taskbar Labels

* bullet one
* bullet two

```
// code sample with // markers
int main() {}
```
*/
// ==/WindhawkModReadme==

// ==WindhawkModSettings==
/*
- mode: labelsWithoutCombining
  $name: Mode
  $description: >-
    Choose how labels are shown
  $options:
    - labelsWithoutCombining: Labels without combining
    - labelsWithCombining: Labels with combining
- spacing: 8
  $name: Spacing
*/
// ==/WindhawkModSettings==
"""


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []

    def get(self, url, *, etag=None, headers=None, timeout=None):
        self.calls.append(url)

        if url.endswith("/catalogs/en.json") or url.endswith("/catalog.json"):
            body = json.dumps(CATALOG).encode()
            return HttpResponse(
                url, 200, {"etag": '"abc"', "content-type": "application/json"}, body
            )

        if url.endswith("/taskbar-labels.versions.json") or url.endswith("/versions.json"):
            body = json.dumps([{"version": "2.4", "timestamp": 1600000000},
                               {"version": "2.5", "timestamp": 1690000000}]).encode()
            return HttpResponse(url, 200, {"content-type": "application/json"}, body)

        if url.endswith("/taskbar-labels.wh.cpp"):
            return HttpResponse(url, 200, {"etag": '"src"'}, SOURCE.encode())

        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    def close(self) -> None:
        return None


def main() -> int:
    transport = FakeTransport()
    client = Client(cache_dir=False, transport=transport)  # type: ignore[arg-type]

    catalog = client.get_catalog()
    assert len(catalog) == 2, len(catalog)
    assert catalog.app.version == "1.5.2"

    aero = catalog.get("aero-tray")
    assert isinstance(aero, Mod)
    assert aero.users == 120000
    assert aero.rating == 4.71, aero.rating  # from breakdown, not the 0-10 int
    assert aero.rating_score == 9
    assert aero.rating_users == 200
    assert aero.updated_at is not None and aero.updated_at.year == 2023
    assert aero.supports_process("explorer")
    assert not aero.supports_process("dwm.exe")
    assert aero.supports_architecture("x86-64")

    # Sorting + filtering.
    top = client.list_mods(limit=1)
    assert top[0].id == "aero-tray"
    assert client.list_mods(process="dwm.exe") == ()

    # Search.
    hits = client.search("taskbar labels")
    assert hits and hits[0].mod.id == "taskbar-labels", [h.mod.id for h in hits]
    assert hits[0].snippet
    corrections = client.did_you_mean("taskbar lables")
    assert corrections, "expected a spelling suggestion for 'lables'"

    # Source parsing.
    parsed = client.parse_source("taskbar-labels")
    assert parsed.mod_id == "taskbar-labels"
    assert parsed.version == "2.5"
    assert parsed.author == "ramen software"
    assert parsed.license_note and "GNU General Public License" in parsed.license_note
    assert parsed.readme and parsed.readme.startswith("# Taskbar Labels")
    assert "* bullet one" in parsed.readme
    assert "// code sample with // markers" in parsed.readme, "readme must stay verbatim"

    settings = parsed.settings
    assert settings.keys() == ("mode", "spacing"), settings.keys()
    mode = settings.get("mode")
    assert mode is not None
    assert mode.default == "labelsWithoutCombining"
    assert mode.name == "Mode"
    assert mode.description == "Choose how labels are shown", mode.description
    assert len(mode.options) == 2
    assert settings.get("spacing").default == "8"

    # Detail merge.
    detail = client.get_mod_detail("taskbar-labels")
    assert detail.readme is not None
    assert detail.mod.version == "2.5"
    assert len(detail.versions) == 2
    assert any(v.is_current for v in detail.versions), detail.versions

    # Versions endpoint.
    versions = client.list_versions("taskbar-labels")
    assert versions[1].published_at.year == 2023

    # Missing mod.
    try:
        client.get_mod("does-not-exist")
    except ModNotFoundError as exc:
        assert exc.mod_id == "does-not-exist"
    else:
        raise AssertionError("expected ModNotFoundError")

    # Parser robustness.
    empty = parse_mod_source("")
    assert empty.malformed and not empty
    # An unclosed block is skipped entirely (extract_blocks pairs open+close).
    weird = parse_mod_source("// ==WindhawkMod==\n// @name X\n// ==/WindhawkMod==\n")
    assert weird.metadata.name == "X" and not weird.malformed
    truncated = parse_mod_source("// ==WindhawkMod==\n// @name X\n")
    assert truncated.metadata.name == "" and truncated.malformed and truncated.blocks == {}

    # to_dict round trip.
    json.dumps(detail.to_dict())
    json.dumps(catalog.to_dict())

    print("offline smoke test OK")
    print("cache stats:", client.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
