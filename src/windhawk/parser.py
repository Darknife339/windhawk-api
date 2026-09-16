"""Parsers for the ``.wh.cpp`` mod format.

A Windhawk mod is a single C++ file whose header carries structured comment
blocks::

    // ==WindhawkMod==
    // @id              taskbar-labels
    // @name            Taskbar Labels for Windows 11
    // @include         explorer.exe
    // ==/WindhawkMod==

    // ==WindhawkModReadme==
    /*
    # Markdown readme
    */
    // ==/WindhawkMod==

    // ==WindhawkModSettings==
    /*
    - mode: labelsWithoutCombining
      $name: Mode
    */
    // ==/WindhawkModSettings==

Note the two different comment styles: the metadata block prefixes every line
with ``//``, while the README and settings blocks are wrapped in a single
``/* ... */`` and their contents are *verbatim* markdown/YAML.  Stripping ``//``
from a README would corrupt embedded code samples, so
:func:`strip_comment_markers` takes an explicit mode instead of guessing.

These blocks are the only reliable source of per-mod documentation, so the
parsers are deliberately forgiving: unknown keys are preserved on
:attr:`ModMetadata.unknown`, missing blocks yield ``None``, and nothing here
raises on odd input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .models import Mod, ModMetadata

__all__ = [
    "ParsedMod",
    "SettingEntry",
    "SettingOption",
    "SettingsBlock",
    "extract_blocks",
    "parse_license_note",
    "parse_metadata_block",
    "parse_mod_source",
    "parse_readme_block",
    "parse_settings_block",
    "strip_comment_markers",
]

#: ``// ==WindhawkModReadme==`` / ``// ==/WindhawkMod==`` / ``/* ==Foo== */``.
#: ``closed`` captures the ``/`` of a closing marker (in ``==/Name==`` the
#: slash precedes the name), and ``\r?`` keeps Windows CRLF sources matchable
#: since the scan runs over the raw text rather than ``splitlines()``.
_BLOCK_RE = re.compile(
    r"^[ \t]*(?://+|/?\*)[ \t]*==(?P<closed>/)?(?P<kind>[A-Za-z][A-Za-z0-9_-]*?)==[ \t]*\r?$",
    re.MULTILINE,
)

#: ``// @name    Some value`` — inner spacing of the value is preserved.
_META_LINE_RE = re.compile(
    r"^[ \t]*(?://+|/?\*)[ \t]*@(?P<key>[A-Za-z][A-Za-z0-9_-]*)[ \t]*(?P<value>.*?)[ \t]*$"
)

#: Repeated-per-line metadata keys (the catalog serves them as arrays).
_LIST_KEYS = frozenset({"include", "exclude", "architecture"})

_LINE_COMMENT_RE = re.compile(r"^[ \t]*//+[ \t]?")

_LICENSE_RE = re.compile(
    r"(?:is published under|licensed under|license:|released under)\s*(?P<license>.+?)\s*$",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Block extraction
# --------------------------------------------------------------------------- #
def extract_blocks(source: str) -> Dict[str, str]:
    """Return every ``==Name==`` block keyed by its block name.

    An opening marker is paired with the next closing marker of the *same*
    name; mismatched or unclosed markers are skipped rather than raising, so a
    truncated download still yields whatever blocks it managed to carry.
    """

    if not source:
        return {}

    blocks: Dict[str, str] = {}
    open_marker: Optional[Tuple[str, int]] = None

    for match in _BLOCK_RE.finditer(source):
        kind = match.group("kind")
        closed = match.group("closed") == "/"

        if not closed:
            if open_marker is None:
                open_marker = (kind, match.end())
            continue

        if open_marker is None:
            continue

        name, start = open_marker
        if name != kind:
            # Mismatched pairing — drop the stale opener and keep scanning.
            open_marker = None
            continue

        blocks.setdefault(name, source[start : match.start()])
        open_marker = None

    return blocks


def strip_comment_markers(block: str, *, line_comments: Optional[bool] = None) -> str:
    """Unwrap a block body.

    Args:
        block: raw text between the ``==Name==`` markers.
        line_comments: ``True`` strips a leading ``//`` from every line (the
            metadata block style), ``False`` leaves lines untouched (the
            ``/* ... */`` style used by README/settings).  ``None`` auto-detects
            from the presence of a ``/*`` wrapper.

    Only the outer ``/*``/``*/`` wrapper and the per-line ``//`` prefixes are
    removed; markdown list markers (``* item``) and code samples are preserved
    byte for byte.
    """

    if not block:
        return ""

    has_block_comment = "/*" in block
    if line_comments is None:
        # Metadata blocks are pure ``//`` runs; README/settings are wrapped.
        line_comments = not has_block_comment

    text = block

    if line_comments:
        text = "\n".join(_LINE_COMMENT_RE.sub("", line.rstrip()) for line in text.splitlines())

    text = text.strip("\n").strip()

    if text.startswith("/*"):
        text = text[2:]
    if text.endswith("*/"):
        text = text[:-2]

    return text.strip("\n").strip()


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #
def parse_metadata_block(block: Optional[str]) -> ModMetadata:
    """Parse a ``==WindhawkMod==`` block into :class:`~windhawk.models.ModMetadata`."""

    if not block:
        return ModMetadata()

    payload: Dict[str, Any] = {}
    lists: Dict[str, List[str]] = {}

    for line in block.splitlines():
        match = _META_LINE_RE.match(line)
        if not match:
            continue

        key = match.group("key")
        value = match.group("value").strip().strip('"')

        if key in _LIST_KEYS:
            lists.setdefault(key, []).extend(p.strip() for p in value.split(",") if p.strip())
            continue

        # First occurrence wins: ``@name`` is not repeated in practice, and if
        # an author duplicates it the first value is the one the site shows.
        payload.setdefault(key, value)

    for key, values in lists.items():
        payload[key] = values

    return ModMetadata.from_payload(payload)


def parse_license_note(source: str, after: Optional[int] = None) -> Optional[str]:
    """Best-effort extraction of the licence mentioned below the header.

    Many mods carry a line such as
    ``// Source code is published under The GNU General Public License v3.0.``
    right after ``==/WindhawkMod==``.  That is prose, not structured data, so a
    miss simply returns ``None``.
    """

    if not source:
        return None

    window = source[after or 0 : (after or 0) + 4000]

    for line in window.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("//"):
            # Only the comment run directly below the header is considered.
            break

        match = _LICENSE_RE.search(stripped)
        if match:
            note = match.group("license").strip().rstrip(".").strip()
            if note:
                return note

    return None


# --------------------------------------------------------------------------- #
# README
# --------------------------------------------------------------------------- #
def parse_readme_block(block: Optional[str]) -> Optional[str]:
    """Return the README markdown, or ``None`` when the block is absent."""

    if not block:
        return None

    text = strip_comment_markers(block, line_comments=False)
    return text or None


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SettingOption:
    """One choice of a ``$options`` list."""

    value: str
    label: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "label": self.label}


@dataclass(frozen=True)
class SettingEntry:
    """One top-level setting of the ``==WindhawkModSettings==`` block."""

    key: str
    default: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    options: Tuple[SettingOption, ...] = ()
    #: Raw text of this entry, useful for rendering the settings UI verbatim.
    raw: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "default": self.default,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "options": [option.to_dict() for option in self.options],
        }


@dataclass(frozen=True)
class SettingsBlock:
    """Parsed ``==WindhawkModSettings==`` block."""

    raw: str = ""
    entries: Tuple[SettingEntry, ...] = ()
    #: ``True`` when a block existed but produced no entries at all.
    malformed: bool = False

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def keys(self) -> Tuple[str, ...]:
        return tuple(entry.key for entry in self.entries)

    def get(self, key: str) -> Optional[SettingEntry]:
        for entry in self.entries:
            if entry.key == key:
                return entry
        return None

    def defaults(self) -> Dict[str, Optional[str]]:
        return {entry.key: entry.default for entry in self.entries}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw": self.raw,
            "malformed": self.malformed,
            "entries": [entry.to_dict() for entry in self.entries],
        }


_KEY_RE = re.compile(r"^(?P<key>[A-Za-z0-9_.-]+):[ \t]*(?P<value>.*)$")
_META_RE = re.compile(r"^\$(?P<key>name|description|options|category)[ \t]*:?[ \t]*(?P<value>.*)$")
_FOLD_MARKERS = frozenset({">-", ">", "|", "|-"})


def _split_inline(value: str) -> List[str]:
    return [part.strip().strip("'\"") for part in value.strip("[]").split(",") if part.strip()]


def _parse_options(
    lines: List[str], index: int, base_indent: int
) -> Tuple[List[SettingOption], int]:
    options: List[SettingOption] = []

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break

        stripped = line.strip()

        if stripped.startswith("- "):
            body = stripped[2:].strip()
            match = _KEY_RE.match(body)
            if match:
                options.append(
                    SettingOption(
                        value=match.group("key"),
                        label=match.group("value").strip() or None,
                    )
                )
            elif body:
                options.append(SettingOption(value=body.strip("'\"")))
        else:
            # Inline flow list continuation: ``$options: [a, b]``.
            options.extend(SettingOption(value=part) for part in _split_inline(stripped))

        index += 1

    return options, index


def _parse_folded(lines: List[str], index: int, base_indent: int) -> Tuple[str, int]:
    """Join a ``>-`` folded scalar back into one line."""

    parts: List[str] = []

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            break

        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break

        parts.append(line.strip())
        index += 1

    return " ".join(parts), index


def parse_settings_block(block: Optional[str]) -> SettingsBlock:
    """Parse the settings block into structured entries.

    The block is hand-written YAML-ish; this scanner understands the documented
    shape (``- key: default`` followed by ``$name`` / ``$description`` /
    ``$options`` / ``$category``) and skips anything else.  The raw text is
    always preserved so a caller can render or re-parse it.
    """

    if not block:
        return SettingsBlock()

    text = strip_comment_markers(block)
    if not text:
        return SettingsBlock()

    lines = text.splitlines()
    entries: List[SettingEntry] = []
    current: Optional[Dict[str, Any]] = None
    entry_start = 0

    def flush(end: int) -> None:
        if not current or not current.get("key"):
            return
        entries.append(
            SettingEntry(
                key=str(current["key"]),
                default=current.get("default"),
                name=current.get("name"),
                description=current.get("description"),
                category=current.get("category"),
                options=tuple(current.get("options", ())),
                raw="\n".join(lines[entry_start:end]).strip(),
            )
        )

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        indent = len(line) - len(line.lstrip())

        if stripped.startswith("- ") and indent == 0:
            flush(index)
            entry_start = index
            current = {}

            match = _KEY_RE.match(stripped[2:].strip())
            if match:
                current["key"] = match.group("key")
                current["default"] = match.group("value").strip().strip("'\"") or None
            else:
                # Not a setting line after all.
                current = None

            index += 1
            continue

        if current is not None:
            meta = _META_RE.match(stripped)
            if meta:
                key = meta.group("key")
                value = meta.group("value").strip()

                if key == "options":
                    if value and value not in _FOLD_MARKERS:
                        current["options"] = [
                            SettingOption(value=part) for part in _split_inline(value)
                        ]
                        index += 1
                    else:
                        options, index = _parse_options(lines, index + 1, indent)
                        current["options"] = options
                    continue

                if value in _FOLD_MARKERS:
                    folded, index = _parse_folded(lines, index + 1, indent)
                    current[key] = folded or None
                    continue

                current[key] = value.strip("'\"") or None
                index += 1
                continue

        index += 1

    flush(len(lines))

    return SettingsBlock(raw=text, entries=tuple(entries), malformed=bool(text) and not entries)


# --------------------------------------------------------------------------- #
# Top level
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ParsedMod:
    """Structured view of a ``.wh.cpp`` file.

    Attributes:
        mod_id: ``@id`` when present, else the id the caller passed in.
        metadata: everything from the ``==WindhawkMod==`` block.
        readme: markdown body of ``==WindhawkModReadme==``.
        settings: parsed ``==WindhawkModSettings==`` block.
        license_note: licence mentioned in prose below the header.
        blocks: every block found, keyed by name — unknown blocks (mods do
            invent their own) stay reachable here.
        malformed: ``True`` when no metadata block was found at all.
    """

    mod_id: str = ""
    metadata: ModMetadata = field(default_factory=ModMetadata)
    readme: Optional[str] = None
    settings: SettingsBlock = field(default_factory=SettingsBlock)
    license_note: Optional[str] = None
    blocks: Mapping[str, str] = field(default_factory=dict, repr=False)
    malformed: bool = False

    def __bool__(self) -> bool:
        return not self.malformed

    @property
    def name(self) -> str:
        return self.metadata.name or self.mod_id

    @property
    def version(self) -> Optional[str]:
        return self.metadata.version

    @property
    def author(self) -> Optional[str]:
        return self.metadata.author

    @property
    def include(self) -> Tuple[str, ...]:
        return self.metadata.include

    def to_mod(self) -> Mod:
        """Build a :class:`~windhawk.models.Mod` from the source alone.

        Catalog-only fields (``users``, ``rating``, timestamps) stay at their
        defaults — merge with a catalog mod via
        :meth:`~windhawk.models.Mod.with_metadata` to get both.
        """

        metadata = self.metadata
        mod_id = self.mod_id or metadata.mod_id or _slug(metadata.name)

        return Mod(
            id=mod_id,
            name=metadata.name or mod_id,
            description=metadata.description,
            author=metadata.author,
            version=metadata.version,
            include=metadata.include,
            exclude=metadata.exclude,
            architecture=metadata.architecture,
            license=metadata.license or self.license_note,
            github=metadata.github,
            homepage=metadata.homepage,
            twitter=metadata.twitter,
            donate_url=metadata.donate_url,
            compiler_options=metadata.compiler_options,
            metadata=metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "metadata": self.metadata.to_dict(),
            "readme": self.readme,
            "settings": self.settings.to_dict(),
            "license_note": self.license_note,
            "malformed": self.malformed,
        }


def parse_mod_source(source: str, mod_id: str = "") -> ParsedMod:
    """Parse a complete ``.wh.cpp`` file.

    Never raises: an empty or unparseable file yields ``ParsedMod(malformed=True)``
    so callers can still surface the raw source to the user.
    """

    if not isinstance(source, str) or not source:
        return ParsedMod(mod_id=mod_id, malformed=True)

    blocks = extract_blocks(source)
    metadata_block = blocks.get("WindhawkMod")
    metadata = parse_metadata_block(metadata_block)

    readme = parse_readme_block(blocks.get("WindhawkModReadme"))
    settings = parse_settings_block(blocks.get("WindhawkModSettings"))

    license_note = None
    if metadata_block is not None:
        closing = re.search(r"==/WindhawkMod==", source)
        license_note = parse_license_note(source, closing.end() if closing else 0)

    if not license_note and metadata.license:
        license_note = metadata.license

    return ParsedMod(
        mod_id=mod_id or (metadata.mod_id or "") or _slug(metadata.name),
        metadata=metadata,
        readme=readme,
        settings=settings,
        license_note=license_note,
        blocks=blocks,
        malformed=metadata_block is None,
    )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")