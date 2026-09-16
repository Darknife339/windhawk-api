"""Shared fixtures: canned repository payloads and a fake transport."""

from __future__ import annotations

import json

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

VERSIONS = [
    {"version": "2.4", "timestamp": 1600000000},
    {"version": "2.5", "timestamp": 1690000000},
]


class FakeTransport:
    """Serves the canned payloads; 404s everything else like the CDN would.

    Raises the library's ``NotFoundError`` (not a bare ``HTTPError``) so the
    client sees the same exception shape as the real transport.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url, *, etag=None, headers=None, timeout=None):  # type: ignore[no-untyped-def]
        self.calls.append(url)

        if url.endswith("/catalogs/en.json") or url.endswith("/catalog.json"):
            body = json.dumps(CATALOG).encode()
            return HttpResponse(
                url, 200, {"etag": '"abc"', "content-type": "application/json"}, body
            )

        if url.endswith("/taskbar-labels/versions.json"):
            body = json.dumps(VERSIONS).encode()
            return HttpResponse(url, 200, {"content-type": "application/json"}, body)

        if url.endswith("/taskbar-labels.wh.cpp"):
            return HttpResponse(url, 200, {"etag": '"src"'}, SOURCE.encode())

        from windhawk.exceptions import NotFoundError

        raise NotFoundError(
            f"Repository returned HTTP 404 for {url}",
            url=url,
            status_code=404,
            response_body="",
            response_headers={},
        )

    def close(self) -> None:
        return None
