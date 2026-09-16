"""Input validation helpers.

The repository URLs are built by interpolating ``mod_id`` and ``version`` into
the path, so these values must never be taken from user input verbatim —
otherwise ``../../admin`` or ``http://evil/`` style payloads become path
traversal / SSRF.  Everything in this module is a hard allow-list check that
runs *before* a request is made.
"""

from __future__ import annotations

import re
from typing import Final

from .exceptions import ValidationError

#: Windhawk mod ids are kebab-case file names (``mods/<id>.wh.cpp``).
MOD_ID_MAX_LENGTH: Final[int] = 200

#: ``mod_id`` allow-list: lowercase/uppercase letters, digits, dot, dash,
#: underscore.  No slash, no backslash, no dot-only segments.
_MOD_ID_RE: Final["re.Pattern[str]"] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,198}[A-Za-z0-9]$")

#: Versions seen in the wild: ``1``, ``1.0``, ``1.2.3``, ``1.0.260614``,
#: ``2.0.0-alpha.5``.
_VERSION_RE: Final["re.Pattern[str]"] = re.compile(
    r"^[0-9]+(?:\.[0-9]+)*(?:[-+][A-Za-z0-9][A-Za-z0-9._-]{0,31})?$"
)

_VERSION_MAX_LENGTH: Final[int] = 64

#: Language tags are plain BCP-47-ish codes: ``en``, ``pt-BR``, ``zh-Hans``.
_LANGUAGE_RE: Final["re.Pattern[str]"] = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")

_LANGUAGE_MAX_LENGTH: Final[int] = 20

#: Characters that must never appear in a query we index or search for.
_CONTROL_RE: Final["re.Pattern[str]"] = re.compile(r"[\x00-\x1f\x7f]")


def validate_mod_id(mod_id: str, *, field_name: str = "mod_id") -> str:
    """Return *mod_id* unchanged when it is safe to embed into a URL path.

    Raises:
        ValidationError: when the value is empty, oversized, contains a path
            separator, or is not a plausible repository file name.
    """

    if not isinstance(mod_id, str):
        raise ValidationError(f"{field_name} must be a string, got {type(mod_id).__name__}")

    value = mod_id.strip()

    if not value:
        raise ValidationError(f"{field_name} must not be empty")
    if len(value) > MOD_ID_MAX_LENGTH:
        raise ValidationError(f"{field_name} is longer than {MOD_ID_MAX_LENGTH} characters")
    if _CONTROL_RE.search(value):
        raise ValidationError(f"{field_name} must not contain control characters")
    if "/" in value or "\\" in value:
        raise ValidationError(f"{field_name} must not contain path separators")
    if ".." in value:
        raise ValidationError(f"{field_name} must not contain '..'")
    if value.startswith(".") or value.endswith("."):
        raise ValidationError(f"{field_name} must not start or end with a dot")
    if not _MOD_ID_RE.match(value):
        raise ValidationError(
            f"{field_name} {value!r} has an unexpected format; expected a repository "
            "identifier such as 'windows-11-taskbar-styler'"
        )

    return value


def validate_version(version: str, *, field_name: str = "version") -> str:
    """Return *version* unchanged when it is safe to embed into a URL path."""

    if not isinstance(version, str):
        raise ValidationError(f"{field_name} must be a string, got {type(version).__name__}")

    value = version.strip()

    if not value:
        raise ValidationError(f"{field_name} must not be empty")
    if len(value) > _VERSION_MAX_LENGTH:
        raise ValidationError(f"{field_name} is longer than {_VERSION_MAX_LENGTH} characters")
    if _CONTROL_RE.search(value):
        raise ValidationError(f"{field_name} must not contain control characters")
    if not _VERSION_RE.match(value):
        raise ValidationError(
            f"{field_name} {value!r} has an unexpected format; expected something like '1.2.3'"
        )

    return value


def validate_language(language: str) -> str:
    """Return *language* normalised to a lowercase tag when it is well formed."""

    if not isinstance(language, str):
        raise ValidationError(f"language must be a string, got {type(language).__name__}")

    value = language.strip()

    if not value:
        raise ValidationError("language must not be empty")
    if len(value) > _LANGUAGE_MAX_LENGTH:
        raise ValidationError(f"language is longer than {_LANGUAGE_MAX_LENGTH} characters")
    if not _LANGUAGE_RE.match(value):
        raise ValidationError(
            f"language {value!r} is not a valid language tag; expected e.g. 'en' or 'pt-BR'"
        )

    return value.lower()


def sanitize_query(query: str, *, max_length: int = 512) -> str:
    """Normalise a free-text search query.

    Never raises: oversized input is truncated and control characters are
    dropped, because a search query is not a security boundary (it is never
    interpolated into a URL).
    """

    if not isinstance(query, str):
        return ""

    value = _CONTROL_RE.sub(" ", query)
    value = " ".join(value.split())
    return value[:max_length]