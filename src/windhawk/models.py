"""Typed models for everything the Windhawk repository exposes.

The JSON served by ``mods.windhawk.net`` has a few quirks that are normalised
once, here, instead of leaking into every caller:

* a catalog is a **mapping** keyed by mod id (``{"mods": {"aero-tray": {...}}}``),
  not a list, and each entry is split into ``metadata`` (author supplied) and
  ``details`` (server computed),
* ``details.rating`` is an integer on a **0-10** scale, while
  ``details.ratingBreakdown`` holds the per-star counts — the breakdown is the
  trustworthy source, so :attr:`Mod.rating` is derived from it and exposed in
  the usual 0-5 star form,
* ``details.published``/``details.updated`` are epoch **milliseconds**, whereas
  ``versions.json`` timestamps are epoch **seconds**.

Every model is a frozen dataclass with ``to_dict()``, so results can be handed
straight to :func:`json.dump`.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from .exceptions import ParseError, ValidationError

__all__ = [
    "AppInfo",
    "Catalog",
    "Mod",
    "ModMetadata",
    "ModSource",
    "SearchResult",
    "VersionInfo",
    "normalize_mod_id",
    "parse_datetime",
    "sort_mods",
]

#: Sort keys accepted by :func:`sort_mods` / ``Catalog.sort`` / the CLI.
SORT_KEYS = ("users", "rating", "updated", "published", "name", "author", "id", "sorting")

#: ``details.ratingBreakdown`` is ``[1★, 2★, 3★, 4★, 5★]`` (verified against the
#: live catalog: the counts always sum to ``details.ratingUsers``).
_BREAKDOWN_ORDER = (1, 2, 3, 4, 5)


# --------------------------------------------------------------------------- #
# Coercion helpers
# --------------------------------------------------------------------------- #
def _first(data: Mapping[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


def as_optional_str(value: Any) -> Optional[str]:
    text = as_str(value).strip()
    return text or None


def as_int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+", value.replace(",", "").replace(" ", ""))
        if match:
            return int(match.group(0))
    return default


def as_float(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%"))
        except ValueError:
            return default
    return default


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return default


def as_str_tuple(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        parts = (part.strip() for part in re.split(r"[,;\n]", value))
        return tuple(part for part in parts if part)
    if isinstance(value, (list, tuple, set)):
        return tuple(as_str(item) for item in value if as_str(item))
    return ()


def as_mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def parse_datetime(value: Any) -> Optional[_dt.datetime]:
    """Parse every timestamp shape the repository is known to emit.

    Accepts epoch milliseconds (catalog), epoch seconds (``versions.json``),
    ISO-8601 strings with or without a timezone, and ``"0000-00-00"`` style
    sentinels (returned as ``None``).  Always returns an aware UTC datetime.
    """

    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, _dt.datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        seconds = float(value)
        # Anything above ~1973-03 in seconds is really milliseconds.
        if seconds > 1e11:
            seconds /= 1000.0
        if seconds <= 0:
            return None
        try:
            parsed = _dt.datetime.fromtimestamp(seconds, tz=_dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text or text.startswith("0000-00-00"):
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = _dt.datetime.fromisoformat(text)
        except ValueError:
            match = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T]?(\d{2}):(\d{2})(?::(\d{2}))?", text)
            if not match:
                return None
            groups = match.groups()
            try:
                parsed = _dt.datetime(
                    int(groups[0]),
                    int(groups[1]),
                    int(groups[2]),
                    int(groups[3]),
                    int(groups[4]),
                    int(groups[5] or 0),
                )
            except ValueError:
                return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def normalize_mod_id(mod_id: str) -> str:
    """Canonicalise an identifier for comparison.

    Strips surrounding whitespace, a trailing ``.wh.cpp`` and lowercases, so
    ``"Aero-Tray.wh.cpp"``, ``"aero-tray"`` and ``" aero-tray "`` all match.
    """

    text = (mod_id or "").strip().lower()
    if text.endswith(".wh.cpp"):
        text = text[: -len(".wh.cpp")]
    return text


# --------------------------------------------------------------------------- #
# Metadata (author supplied)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModMetadata:
    """Author supplied ``metadata`` block.

    The same shape is produced from two sources: the catalog's ``metadata``
    object and the ``// ==WindhawkMod==`` comment block of a ``.wh.cpp`` file
    (whose keys carry an ``@`` prefix).  :meth:`from_payload` accepts both.

    Attributes:
        name / description / version / author: the four fields every mod has.
        github / homepage / twitter / donate_url: author links.
        license: SPDX-ish string when the author declared one.
        include / exclude: processes the mod injects into / skips.  ``"*"``
            means "every process".
        architecture: supported architectures (``x86``, ``x86-64``, ``arm64``).
        compiler_options: extra flags handed to the Windhawk build.
        unknown: keys this version of the library does not know about, kept so
            a new upstream field never silently disappears.
    """

    name: str = ""
    description: str = ""
    version: Optional[str] = None
    author: Optional[str] = None
    github: Optional[str] = None
    homepage: Optional[str] = None
    twitter: Optional[str] = None
    donate_url: Optional[str] = None
    license: Optional[str] = None
    include: Tuple[str, ...] = ()
    exclude: Tuple[str, ...] = ()
    architecture: Tuple[str, ...] = ()
    compiler_options: Optional[str] = None
    mod_id: Optional[str] = None
    unknown: Mapping[str, Any] = field(default_factory=dict, repr=False)

    _KNOWN_KEYS = frozenset(
        {
            "id",
            "name",
            "description",
            "version",
            "author",
            "github",
            "homepage",
            "twitter",
            "donateUrl",
            "donate_url",
            "license",
            "include",
            "exclude",
            "architecture",
            "architectures",
            "compilerOptions",
            "compiler_options",
        }
    )

    @classmethod
    def from_payload(cls, data: Any) -> "ModMetadata":
        """Build metadata from a catalog object or an ``@``-keyed block."""

        payload = as_mapping(data)
        if not payload:
            return cls()

        lowered = {str(key).lstrip("@"): value for key, value in payload.items()}
        unknown = {
            key: value for key, value in lowered.items() if key not in cls._KNOWN_KEYS
        }

        return cls(
            name=as_str(_first(lowered, ("name",))),
            description=as_str(_first(lowered, ("description",))),
            version=as_optional_str(lowered.get("version")),
            author=as_optional_str(lowered.get("author")),
            github=as_optional_str(lowered.get("github")),
            homepage=as_optional_str(lowered.get("homepage")),
            twitter=as_optional_str(lowered.get("twitter")),
            donate_url=as_optional_str(_first(lowered, ("donateUrl", "donate_url", "donate"))),
            license=as_optional_str(lowered.get("license")),
            include=as_str_tuple(lowered.get("include")),
            exclude=as_str_tuple(lowered.get("exclude")),
            architecture=as_str_tuple(_first(lowered, ("architecture", "architectures"))),
            compiler_options=as_optional_str(
                _first(lowered, ("compilerOptions", "compiler_options"))
            ),
            mod_id=as_optional_str(lowered.get("id")),
            unknown=unknown,
        )

    #: Alias kept for readability when parsing ``.wh.cpp`` blocks.
    parse = from_payload

    def merged_with(self, other: "ModMetadata") -> "ModMetadata":
        """Return metadata where *other* fills only our empty fields."""

        updates: Dict[str, Any] = {}
        for name in (
            "name",
            "description",
            "version",
            "author",
            "github",
            "homepage",
            "twitter",
            "donate_url",
            "license",
            "compiler_options",
            "mod_id",
        ):
            if not getattr(self, name) and getattr(other, name):
                updates[name] = getattr(other, name)

        for name in ("include", "exclude", "architecture"):
            if not getattr(self, name) and getattr(other, name):
                updates[name] = getattr(other, name)

        unknown = dict(self.unknown)
        unknown.update(other.unknown)
        updates["unknown"] = unknown

        return replace(self, **updates)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "github": self.github,
            "homepage": self.homepage,
            "twitter": self.twitter,
            "donate_url": self.donate_url,
            "license": self.license,
            "include": list(self.include),
            "exclude": list(self.exclude),
            "architecture": list(self.architecture),
            "compiler_options": self.compiler_options,
            "id": self.mod_id,
        }


# --------------------------------------------------------------------------- #
# Mod
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Mod:
    """A single mod as listed in the catalog.

    Attributes:
        id: URL identifier (``aero-tray``), i.e. the catalog key.
        name / description / author: display fields.
        users: how many installs have the mod enabled — the best popularity
            signal the repository publishes.
        rating: average in **0-5** stars, derived from ``ratingBreakdown``.
        rating_score: the raw server value (0-10 integer).
        rating_users: how many users rated the mod.
        rating_breakdown: ``[1★, 2★, 3★, 4★, 5★]`` counts.
        published_at / updated_at: aware UTC datetimes.
        default_sorting: the site's own ranking weight.
        include / exclude / architecture: injection targets and support matrix.
        license / github / homepage / twitter / donate_url: author fields.
        compiler_options: build flags from the author's metadata block.
    """

    id: str
    name: str = ""
    description: str = ""
    author: Optional[str] = None
    version: Optional[str] = None
    users: int = 0
    rating: float = 0.0
    rating_score: int = 0
    rating_users: int = 0
    rating_breakdown: Tuple[int, ...] = ()
    published_at: Optional[_dt.datetime] = None
    updated_at: Optional[_dt.datetime] = None
    default_sorting: int = 0
    include: Tuple[str, ...] = ()
    exclude: Tuple[str, ...] = ()
    architecture: Tuple[str, ...] = ()
    license: Optional[str] = None
    github: Optional[str] = None
    homepage: Optional[str] = None
    twitter: Optional[str] = None
    donate_url: Optional[str] = None
    compiler_options: Optional[str] = None
    metadata: ModMetadata = field(default_factory=ModMetadata)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    # -- parsing ---------------------------------------------------------- #
    @classmethod
    def from_catalog_entry(cls, mod_id: str, entry: Any) -> "Mod":
        """Build a mod from ``catalog["mods"][mod_id]``.

        Raises:
            ValidationError: when *mod_id* is empty or the entry is not a
                mapping — a malformed catalog must fail loudly rather than
                yield a mod with no identifier.
        """

        identifier = normalize_mod_id(as_str(mod_id))

        if not identifier:
            raise ValidationError("Catalog entry has an empty id")
        if not isinstance(entry, Mapping):
            raise ValidationError(f"Catalog entry {identifier!r} is not an object")

        metadata = ModMetadata.from_payload(entry.get("metadata"))
        details = as_mapping(entry.get("details"))

        breakdown = tuple(as_int(item) for item in (details.get("ratingBreakdown") or ()))
        rating_users = as_int(_first(details, ("ratingUsers", "rating_users")))
        if not rating_users and breakdown:
            rating_users = sum(breakdown)

        rating = _stars_from_breakdown(breakdown)
        if rating is None:
            # No usable breakdown: fall back to the server's 0-10 integer.
            rating = round(min(10.0, max(0.0, as_float(details.get("rating")))) / 2.0, 2)

        return cls(
            id=identifier,
            name=metadata.name or identifier,
            description=metadata.description,
            author=metadata.author,
            version=metadata.version,
            users=as_int(details.get("users")),
            rating=rating,
            rating_score=as_int(details.get("rating")),
            rating_users=rating_users,
            rating_breakdown=breakdown,
            published_at=parse_datetime(_first(details, ("published", "published_at"))),
            updated_at=parse_datetime(_first(details, ("updated", "updated_at"))),
            default_sorting=as_int(_first(details, ("defaultSorting", "default_sorting"))),
            include=metadata.include,
            exclude=metadata.exclude,
            architecture=metadata.architecture,
            license=metadata.license,
            github=metadata.github,
            homepage=metadata.homepage,
            twitter=metadata.twitter,
            donate_url=metadata.donate_url,
            compiler_options=metadata.compiler_options,
            metadata=metadata,
            raw=dict(entry),
        )

    @classmethod
    def parse(cls, data: Any) -> "Mod":
        """Parse either a catalog entry or a flat ``{"id": ..., ...}`` mapping."""

        payload = as_mapping(data)
        mod_id = as_optional_str(_first(payload, ("id", "mod_id", "@id", "filename")))

        if not mod_id and payload.get("filename"):
            mod_id = normalize_mod_id(as_str(payload["filename"]))

        if not mod_id:
            raise ValidationError(
                "Catalog entry has no 'id' field", payload={"keys": sorted(payload)}
            )

        if "metadata" in payload or "details" in payload:
            return cls.from_catalog_entry(mod_id, payload)

        metadata = ModMetadata.from_payload(payload)
        return cls(
            id=normalize_mod_id(mod_id),
            name=metadata.name or normalize_mod_id(mod_id),
            description=metadata.description,
            author=metadata.author,
            version=metadata.version,
            users=as_int(_first(payload, ("users", "user_count", "userCount", "installs"))),
            rating=round(min(5.0, max(0.0, as_float(_first(payload, ("rating",))))), 2),
            rating_users=as_int(_first(payload, ("ratingUsers", "rating_users", "ratings"))),
            published_at=parse_datetime(
                _first(payload, ("published", "published_at", "createdAt", "created_at"))
            ),
            updated_at=parse_datetime(
                _first(payload, ("updated", "updated_at", "updatedAt", "last_updated"))
            ),
            include=metadata.include,
            exclude=metadata.exclude,
            architecture=metadata.architecture,
            license=metadata.license,
            github=metadata.github,
            homepage=metadata.homepage,
            twitter=metadata.twitter,
            donate_url=metadata.donate_url,
            compiler_options=metadata.compiler_options,
            metadata=metadata,
            raw=payload,
        )

    @classmethod
    def parse_many(cls, data: Any) -> Tuple["Mod", ...]:
        """Parse a catalog mapping or list, skipping unusable entries.

        The repository is community edited; one broken entry must not take the
        whole catalog down, so malformed entries are dropped instead of raising.
        """

        if isinstance(data, Mapping):
            inner = data.get("mods")
            if isinstance(inner, Mapping):
                items: Iterable[Tuple[Any, Any]] = inner.items()
            elif isinstance(inner, (list, tuple)):
                items = ((_entry_id(entry), entry) for entry in inner)
            else:
                # A bare ``{"id": ..., ...}`` object describing a single mod.
                items = [(_entry_id(data), data)]
        elif isinstance(data, (list, tuple)):
            items = ((_entry_id(entry), entry) for entry in data)
        else:
            return ()

        mods: List[Mod] = []
        seen = set()

        for mod_id, entry in items:
            try:
                mod = cls.from_catalog_entry(mod_id, entry)
            except ValidationError:
                continue
            if mod.id in seen:
                continue
            seen.add(mod.id)
            mods.append(mod)

        return tuple(mods)

    # -- derived ---------------------------------------------------------- #
    @property
    def download_url(self) -> str:
        from .urls import mod_source_url

        return mod_source_url(self.id)

    @property
    def versions_api_url(self) -> str:
        from .urls import versions_url

        return versions_url(self.id)

    @property
    def page_url(self) -> str:
        from .urls import mod_page_url

        return mod_page_url(self.id)

    @property
    def github_source_url(self) -> str:
        from .urls import github_source_url

        return github_source_url(self.id)

    @property
    def supports_every_process(self) -> bool:
        return "*" in self.include

    @property
    def is_rated(self) -> bool:
        return self.rating_users > 0

    def supports_process(self, process: str) -> bool:
        """Whether the mod injects into *process* (``explorer``, ``dwm.exe``...)."""

        wanted = (process or "").strip().casefold().removesuffix(".exe")
        if not wanted:
            return False
        if self.supports_every_process:
            return True
        for entry in self.include:
            name = entry.strip().casefold().removesuffix(".exe")
            if name == wanted or name.endswith("\\" + wanted):
                return True
        return False

    def supports_architecture(self, architecture: str) -> bool:
        if not self.architecture:
            return True  # Unspecified means "not restricted".
        wanted = (architecture or "").strip().casefold()
        return any(item.casefold() == wanted for item in self.architecture)

    def is_compatible(
        self,
        *,
        architecture: Optional[str] = None,
        process: Optional[str] = None,
    ) -> bool:
        """Filter helper combining :meth:`supports_process` / ``supports_architecture``."""

        if architecture and not self.supports_architecture(architecture):
            return False
        return not (process and not self.supports_process(process))

    def with_metadata(self, metadata: ModMetadata) -> "Mod":
        """Return a copy enriched with ``.wh.cpp`` metadata (source wins)."""

        merged = metadata.merged_with(self.metadata)
        return replace(
            self,
            name=merged.name or self.name,
            description=merged.description or self.description,
            author=merged.author or self.author,
            version=merged.version or self.version,
            include=merged.include or self.include,
            exclude=merged.exclude or self.exclude,
            architecture=merged.architecture or self.architecture,
            license=merged.license or self.license,
            github=merged.github or self.github,
            homepage=merged.homepage or self.homepage,
            twitter=merged.twitter or self.twitter,
            donate_url=merged.donate_url or self.donate_url,
            compiler_options=merged.compiler_options or self.compiler_options,
            metadata=merged,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "users": self.users,
            "rating": self.rating,
            "rating_score": self.rating_score,
            "rating_users": self.rating_users,
            "rating_breakdown": list(self.rating_breakdown),
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "include": list(self.include),
            "exclude": list(self.exclude),
            "architecture": list(self.architecture),
            "license": self.license,
            "github": self.github,
            "homepage": self.homepage,
            "twitter": self.twitter,
            "donate_url": self.donate_url,
            "compiler_options": self.compiler_options,
            "source_url": self.download_url,
            "versions_url": self.versions_api_url,
            "page_url": self.page_url,
        }


def _entry_id(data: Any) -> str:
    payload = as_mapping(data)
    return as_str(_first(payload, ("id", "mod_id", "@id", "filename")))


def _stars_from_breakdown(breakdown: Sequence[int]) -> Optional[float]:
    """Convert ``[1★..5★]`` counts into a 0-5 average, or ``None`` if unusable."""

    if not breakdown:
        return None

    counts = list(breakdown[:5])
    if len(counts) < 5:
        counts.extend([0] * (5 - len(counts)))

    total = sum(counts)
    if total <= 0:
        return 0.0

    weighted = sum(stars * as_int(count) for stars, count in zip(_BREAKDOWN_ORDER, counts))
    return round(weighted / total, 2)


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VersionInfo:
    """One element of ``/mods/{mod_id}/versions.json``.

    Attributes:
        version: version string as published (``"1.0.2"``).
        published_at: ``timestamp`` is epoch **seconds** here, unlike the
            catalog's milliseconds.
        is_current: filled in by the client when it matches the catalog version.
    """

    version: str
    published_at: Optional[_dt.datetime] = None
    timestamp: int = 0
    is_current: bool = False

    @classmethod
    def parse(cls, data: Any) -> "VersionInfo":
        payload = as_mapping(data)
        version = as_optional_str(_first(payload, ("version", "ver", "tag"))) or ""
        timestamp = as_int(_first(payload, ("timestamp", "time", "ts")))
        return cls(
            version=version,
            published_at=parse_datetime(timestamp) if timestamp else None,
            timestamp=timestamp,
        )

    @classmethod
    def parse_many(cls, data: Any) -> Tuple["VersionInfo", ...]:
        if isinstance(data, Mapping):
            for key in ("versions", "data", "items"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
            else:
                data = [data]

        if not isinstance(data, (list, tuple)):
            return ()

        versions: List[VersionInfo] = []
        seen = set()
        for entry in data:
            info = cls(version=entry) if isinstance(entry, str) else cls.parse(entry)
            if not info.version or info.version in seen:
                continue
            seen.add(info.version)
            versions.append(info)

        return tuple(versions)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "timestamp": self.timestamp,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "is_current": self.is_current,
        }


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AppInfo:
    """The catalog's ``app`` block — the published Windhawk releases."""

    version: Optional[str] = None
    version_bleeding_edge: Optional[str] = None
    version_pre_release: Optional[str] = None

    @classmethod
    def parse(cls, data: Any) -> "AppInfo":
        payload = as_mapping(data)
        return cls(
            version=as_optional_str(payload.get("version")),
            version_bleeding_edge=as_optional_str(payload.get("versionBleedingEdge")),
            version_pre_release=as_optional_str(payload.get("versionPreRelease")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "versionBleedingEdge": self.version_bleeding_edge,
            "versionPreRelease": self.version_pre_release,
        }


@dataclass(frozen=True)
class Catalog:
    """An immutable, queryable catalog snapshot."""

    mods: Tuple[Mod, ...] = ()
    app: AppInfo = field(default_factory=AppInfo)
    language: Optional[str] = None
    source: Optional[str] = None
    fetched_at: Optional[_dt.datetime] = None
    from_cache: bool = False

    # -- construction ----------------------------------------------------- #
    @classmethod
    def parse(
        cls,
        payload: Any,
        *,
        language: Optional[str] = None,
        source: Optional[str] = None,
        fetched_at: Optional[_dt.datetime] = None,
        from_cache: bool = False,
    ) -> "Catalog":
        """Build a catalog from a decoded ``/catalogs/<lang>.json`` body.

        Raises:
            ParseError: a CDN error page or truncated body must not masquerade
                as an empty catalog, so a payload that advertises entries but
                yields none is an error rather than a silent ``Catalog()``.
        """

        if not isinstance(payload, Mapping):
            raise ParseError(
                "Catalog payload is not a JSON object",
                payload={"type": type(payload).__name__},
            )

        mods = Mod.parse_many(payload)
        if not mods and isinstance(payload.get("mods"), Mapping) and payload["mods"]:
            raise ParseError("Catalog contains entries but none could be parsed")

        return cls(
            mods=mods,
            app=AppInfo.parse(payload.get("app")),
            language=language,
            source=source,
            fetched_at=fetched_at,
            from_cache=from_cache,
        )

    # -- container protocol ----------------------------------------------- #
    def __len__(self) -> int:
        return len(self.mods)

    def __iter__(self) -> "Iterator[Mod]":
        return iter(self.mods)

    def __bool__(self) -> bool:
        return bool(self.mods)

    def __contains__(self, mod_id: object) -> bool:
        return isinstance(mod_id, str) and self.get(mod_id) is not None

    def __getitem__(self, key):  # type: ignore[no-untyped-def]
        if isinstance(key, int):
            return self.mods[key]
        if isinstance(key, str):
            mod = self.get(key)
            if mod is None:
                raise KeyError(key)
            return mod
        raise TypeError(f"Catalog indices must be int or str, got {type(key).__name__}")

    # -- queries ---------------------------------------------------------- #
    @property
    def ids(self) -> Tuple[str, ...]:
        return tuple(mod.id for mod in self.mods)

    @property
    def total_users(self) -> int:
        return sum(mod.users for mod in self.mods)

    @property
    def authors(self) -> Tuple[str, ...]:
        seen: Dict[str, None] = {}
        for mod in self.mods:
            if mod.author:
                seen.setdefault(mod.author, None)
        return tuple(seen)

    @property
    def processes(self) -> Tuple[str, ...]:
        seen: Dict[str, None] = {}
        for mod in self.mods:
            for entry in mod.include:
                seen.setdefault(entry, None)
        return tuple(seen)

    def get(self, mod_id: str) -> Optional[Mod]:
        """Look a mod up by id, tolerating case and a ``.wh.cpp`` suffix."""

        wanted = normalize_mod_id(mod_id)
        for mod in self.mods:
            if mod.id == wanted:
                return mod
        return None

    def filter(
        self,
        *,
        process: Optional[str] = None,
        author: Optional[str] = None,
        architecture: Optional[str] = None,
        min_users: int = 0,
        min_rating: float = 0.0,
        predicate: Optional[Callable[[Mod], bool]] = None,
    ) -> "Catalog":
        """Return a narrowed catalog (chainable with :meth:`sort`)."""

        mods: List[Mod] = []

        for mod in self.mods:
            if mod.users < min_users or mod.rating < min_rating:
                continue
            if author and author.casefold() not in (mod.author or "").casefold():
                continue
            if not mod.is_compatible(architecture=architecture, process=process):
                continue
            if predicate is not None and not predicate(mod):
                continue
            mods.append(mod)

        return replace(self, mods=tuple(mods))

    def sort(self, by: str = "users", *, reverse: bool = True) -> "Catalog":
        return replace(self, mods=sort_mods(self.mods, by=by, reverse=reverse))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "count": len(self.mods),
            "language": self.language,
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "from_cache": self.from_cache,
            "app": self.app.to_dict(),
            "total_users": self.total_users,
            "mods": [mod.to_dict() for mod in self.mods],
        }


# --------------------------------------------------------------------------- #
# Raw source + search results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModSource:
    """A ``.wh.cpp`` file plus the cache context it arrived with.

    This is the *raw* download; run :func:`windhawk.parse_mod_source` on
    :attr:`content` to get structured metadata, README and settings.
    """

    mod_id: str
    content: str
    url: str = ""
    version: Optional[str] = None
    fetched_at: Optional[_dt.datetime] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    size: int = 0
    from_cache: bool = False

    def __str__(self) -> str:
        return self.content

    def __len__(self) -> int:
        return len(self.content)

    def lines(self) -> Tuple[str, ...]:
        return tuple(self.content.splitlines())

    @property
    def parsed(self):  # type: ignore[no-untyped-def]
        """Lazily parsed :class:`~windhawk.parser.ParsedMod`."""

        from .parser import parse_mod_source

        return parse_mod_source(self.content, self.mod_id)

    @property
    def includes(self) -> Tuple[str, ...]:
        """``#include`` directives found in the C++ body."""

        return tuple(
            line.split("include", 1)[1].strip()
            for line in self.lines()
            if line.lstrip().startswith("#include")
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "version": self.version,
            "url": self.url,
            "size": self.size or len(self.content),
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "from_cache": self.from_cache,
            "content": self.content,
        }


@dataclass(frozen=True)
class SearchResult:
    """One ranked hit from :class:`windhawk.search.ModIndex`."""

    mod: Mod
    score: float = 0.0
    matched_fields: Tuple[str, ...] = ()
    snippet: Optional[str] = None

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        # ``result.name`` / ``result.users`` read naturally in templates.
        try:
            return getattr(self.mod, name)
        except AttributeError:
            raise AttributeError(name) from None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod": self.mod.to_dict(),
            "score": self.score,
            "matched_fields": list(self.matched_fields),
            "snippet": self.snippet,
        }


# --------------------------------------------------------------------------- #
# Sorting
# --------------------------------------------------------------------------- #
def sort_mods(mods: Iterable[Mod], by: str = "users", *, reverse: bool = True) -> Tuple[Mod, ...]:
    """Sort *mods* by one of :data:`SORT_KEYS`.

    Text keys (``name``, ``author``, ``id``) always sort A→Z; numeric keys
    default to highest-first.  An unknown key raises
    :class:`~windhawk.exceptions.ValidationError` instead of silently falling
    back, so a typo in a caller is visible immediately.
    """

    key = (by or "users").strip().casefold()

    if key not in SORT_KEYS:
        raise ValidationError(
            f"Cannot sort mods by {by!r}", payload={"supported": list(SORT_KEYS)}
        )

    epoch = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
    text_key = key in ("name", "author", "id")

    def sort_value(mod: Mod) -> Any:
        if key == "name":
            return (mod.name or mod.id).casefold()
        if key == "id":
            return mod.id
        if key == "author":
            return (mod.author or "").casefold()
        if key == "updated":
            return mod.updated_at or epoch
        if key == "published":
            return mod.published_at or epoch
        if key == "sorting":
            return mod.default_sorting
        if key == "rating":
            # Weight the average by how many votes back it up, so a single 5★
            # does not outrank a mod with 300 ratings at 4.8.
            return mod.rating * min(1.0, mod.rating_users / 10.0)
        return mod.users

    descending = reverse if not text_key else False
    return tuple(sorted(mods, key=sort_value, reverse=descending))