"""In-memory search index over the Windhawk catalog.

The repository exposes no ``/search`` endpoint, so searching is done locally:
the catalog is fetched once, tokenised into an inverted index, and every query
is answered from memory.  That makes the index cheap enough for a Discord bot
or a web search box that answers thousands of queries per minute.

Ranking combines three signals:

1. **Field weight** — a hit in ``id``/``name`` beats a hit in ``description``.
2. **Term coverage** — queries whose terms all match outrank partial matches.
3. **Popularity prior** — user counts act as a small tie-breaker so that the
   well known mod wins when two mods match equally well.
"""

from __future__ import annotations

import math
import re
import threading
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

from .models import Catalog, Mod, SearchResult, sort_mods

__all__ = ["ModIndex", "SearchFilters", "tokenize"]

#: Relative importance of each indexed field.
FIELD_WEIGHTS: Mapping[str, float] = {
    "id": 6.0,
    "name": 5.0,
    "description": 2.0,
    "author": 1.5,
    "include": 1.2,
    "readme": 0.4,
}

#: A term that appears in this fraction of mods is not discriminating enough
#: to be worth much (``the``, ``windows``, ``taskbar`` ...).
_RARITY_FLOOR = 0.0005

_WORD_RE = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)
_SPLIT_RE = re.compile(r"[^0-9A-Za-z\u00c0-\uffff]+")


def tokenize(text: Optional[str]) -> Tuple[str, ...]:
    """Split *text* into lowercase index terms.

    Identifiers such as ``taskbar-labels`` produce both the whole token and its
    parts, so searching ``taskbar`` finds ``taskbar-labels`` and vice versa.
    """

    if not text:
        return ()

    normalized = unicodedata.normalize("NFKC", text).casefold()
    terms: List[str] = []

    for chunk in _SPLIT_RE.split(normalized):
        if not chunk:
            continue
        terms.append(chunk)

        # ``windows11`` -> ``windows11`` only; ``alt-tab`` -> ``alt``, ``tab``.
        for word in _WORD_RE.findall(chunk):
            if word != chunk:
                terms.append(word)

    seen: Set[str] = set()
    unique: List[str] = []
    for term in terms:
        if term and term not in seen and len(term) <= 64:
            seen.add(term)
            unique.append(term)

    return tuple(unique)


@dataclass(frozen=True)
class SearchFilters:
    """Optional constraints applied after scoring.

    Attributes:
        process: keep mods whose ``@include`` list contains this process
            (``"*"`` counts as a match for everything).
        author: case-insensitive substring match on the author field.
        architecture: keep mods supporting this architecture (``x86-64``...).
        min_users / min_rating: popularity thresholds.
        has_readme: require a README block (only meaningful for indexed
            sources, see :meth:`ModIndex.add_source`).
    """

    process: Optional[str] = None
    author: Optional[str] = None
    architecture: Optional[str] = None
    min_users: Optional[int] = None
    min_rating: Optional[float] = None
    exclude_ids: Tuple[str, ...] = ()

    def allows(self, mod: Mod) -> bool:
        if self.exclude_ids and mod.id in self.exclude_ids:
            return False

        if self.min_users is not None and mod.users < self.min_users:
            return False

        if self.min_rating is not None and mod.rating < self.min_rating:
            return False

        if self.author and self.author.casefold() not in (mod.author or "").casefold():
            return False

        if self.architecture:
            wanted = self.architecture.casefold()
            if not any(item.casefold() == wanted for item in mod.metadata.architecture):
                return False

        if self.process:
            wanted = self.process.casefold()
            include = tuple(item.casefold() for item in mod.include)
            # ``*`` means "every process"; otherwise require an exact (or
            # path-suffixed) process name match.
            if "*" not in include and not any(
                item == wanted or item.endswith("\\" + wanted) for item in include
            ):
                return False

        return True


@dataclass
class _Document:
    mod: Mod
    lengths: Dict[str, int]
    terms: Dict[str, Set[str]]


class ModIndex:
    """Inverted index over a :class:`~windhawk.models.Catalog`."""

    def __init__(self, catalog: Optional[Catalog] = None) -> None:
        self._lock = threading.RLock()
        self._docs: Dict[str, _Document] = {}
        self._postings: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        self._total_length: Dict[str, int] = defaultdict(int)
        self._readme_ids: Set[str] = set()
        self.catalog: Optional[Catalog] = catalog

        if catalog is not None:
            self.build(catalog)

    # -- construction ----------------------------------------------------- #
    def build(self, catalog: Catalog) -> "ModIndex":
        """(Re)build the index from *catalog*, replacing any previous content."""

        with self._lock:
            self._reset_locked()
            self.catalog = catalog
            for mod in catalog.mods:
                self._add_mod_locked(mod)

        return self

    def add_source(self, mod_id: str, readme: Optional[str]) -> None:
        """Attach README text to an already indexed mod.

        READMEs live in the ``.wh.cpp`` file rather than in the catalog, so a
        crawler can enrich the index incrementally (e.g. overnight) to make
        full-text search possible.
        """

        if not readme:
            return

        with self._lock:
            document = self._docs.get(mod_id)
            if document is None:
                return

            terms = set(tokenize(readme))
            document.terms["readme"] = terms
            document.lengths["readme"] = len(terms)
            self._readme_ids.add(mod_id)

            for term in terms:
                postings = self._postings[term].get(mod_id)
                if postings is None:
                    self._postings[term][mod_id] = {"readme": 1}
                    self._total_length["readme"] += 1
                elif "readme" not in postings:
                    postings["readme"] = 1

    def add_mod(self, mod: Mod) -> None:
        with self._lock:
            if mod.id in self._docs:
                self._remove_mod_locked(mod.id)
            self._add_mod_locked(mod)

    def _reset_locked(self) -> None:
        self._docs.clear()
        self._postings.clear()
        self._total_length.clear()
        self._readme_ids.clear()

    def _remove_mod_locked(self, mod_id: str) -> None:
        document = self._docs.pop(mod_id, None)
        if document is None:
            return

        for field_name, terms in document.terms.items():
            for term in terms:
                postings = self._postings.get(term)
                if postings and mod_id in postings:
                    postings[mod_id].pop(field_name, None)
                    if not postings[mod_id]:
                        del postings[mod_id]
                    if not postings:
                        del self._postings[term]
                    self._total_length[field_name] = max(0, self._total_length[field_name] - 1)

        self._readme_ids.discard(mod_id)

    def _add_mod_locked(self, mod: Mod) -> None:
        fields: Dict[str, str] = {
            "id": mod.id.replace("-", " ").replace("_", " "),
            "name": mod.name,
            "description": mod.description,
            "author": mod.author or "",
            "include": " ".join(mod.include),
        }

        document = _Document(mod=mod, lengths={}, terms={})

        for field_name, text in fields.items():
            terms = set(tokenize(text))
            document.terms[field_name] = terms
            document.lengths[field_name] = len(terms)
            self._total_length[field_name] += len(terms)

            for term in terms:
                self._postings[term][mod_id_key(mod)][field_name] = 1

        self._docs[mod.id] = document

    # -- queries ---------------------------------------------------------- #
    def __len__(self) -> int:
        with self._lock:
            return len(self._docs)

    @property
    def terms(self) -> int:
        with self._lock:
            return len(self._postings)

    @property
    def mod_ids(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(self._docs)

    def get(self, mod_id: str) -> Optional[Mod]:
        with self._lock:
            document = self._docs.get(mod_id)
            return document.mod if document else None

    def all_mods(self) -> Tuple[Mod, ...]:
        with self._lock:
            return tuple(document.mod for document in self._docs.values())

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: Optional[SearchFilters] = None,
        sort: Optional[str] = None,
        prefix: bool = True,
    ) -> List[SearchResult]:
        """Search the index.

        Args:
            query: free text; empty queries return the whole (filtered) index.
            limit: maximum number of results.
            filters: post-scoring constraints.
            sort: when given (``users``, ``rating``, ``updated``, ``name``,
                ``published``) the matches are ordered by that key instead of
                by relevance.
            prefix: also match terms by prefix (``taskb`` finds ``taskbar``).

        Returns:
            Ranked :class:`~windhawk.models.SearchResult` list.
        """

        from .validation import sanitize_query

        cleaned = sanitize_query(query)
        limit = max(1, int(limit))
        wanted = set(tokenize(cleaned))

        with self._lock:
            documents = list(self._docs.values())

            if not wanted:
                matches = [SearchResult(mod=document.mod, score=float(document.mod.users))
                           for document in documents]
            else:
                matches = self._score_locked(wanted, prefix=prefix)

        if filters is not None:
            matches = [result for result in matches if filters.allows(result.mod)]

        if sort:
            mods = sort_mods([result.mod for result in matches], by=sort)
            scores = {result.mod.id: result for result in matches}
            matches = [scores[mod.id] for mod in mods[:limit]]
        else:
            matches.sort(key=lambda result: (-result.score, -result.mod.users, result.mod.id))
            matches = matches[:limit]

        return [
            SearchResult(
                mod=result.mod,
                score=round(result.score, 4),
                matched_fields=result.matched_fields,
                snippet=result.snippet or _snippet(result.mod, wanted),
            )
            for result in matches
        ]

    def _score_locked(
        self, wanted: Set[str], *, prefix: bool
    ) -> List[SearchResult]:
        total_docs = max(1, len(self._docs))
        {
            field_name: (self._total_length[field_name] / total_docs) or 1.0
            for field_name in FIELD_WEIGHTS
        }

        scores: Dict[str, float] = defaultdict(float)
        hits: Dict[str, Set[str]] = defaultdict(set)
        matched_fields: Dict[str, Set[str]] = defaultdict(set)
        covered_terms: Dict[str, Set[str]] = defaultdict(set)

        for term in wanted:
            candidates: Dict[str, Set[str]] = {}

            postings = self._postings.get(term)
            if postings:
                for hit_mod_id, hit_fields in postings.items():
                    candidates.setdefault(hit_mod_id, set()).update(hit_fields)

            if prefix and len(term) >= 3:
                for index_term, index_postings in self._postings.items():
                    if index_term == term or not index_term.startswith(term):
                        continue
                    # Prefix hits are diluted: the longer the gap, the weaker.
                    for prefix_mod_id, prefix_fields in index_postings.items():
                        candidates.setdefault(prefix_mod_id, set()).update(prefix_fields)

            if not candidates:
                continue

            # Inverse document frequency: rare terms are worth more.
            df = max(1, len(candidates))
            idf = math.log(1.0 + (total_docs / df))

            for mod_id, fields in candidates.items():
                document = self._docs.get(mod_id)
                if document is None:
                    continue

                exact = term in document.terms.get("id", set()) or any(
                    term in document.terms.get(field_name, set()) for field_name in FIELD_WEIGHTS
                )
                term_weight = 1.0 if exact else 0.55

                field_score = 0.0
                for field_name in fields:
                    weight = FIELD_WEIGHTS.get(field_name, 1.0)
                    length = document.lengths.get(field_name, 0) or 1
                    # Length normalisation keeps long descriptions from
                    # dominating short names.
                    norm = 0.5 + 0.5 * (1.0 / math.sqrt(length))
                    field_score += weight * norm

                scores[mod_id] += idf * term_weight * field_score
                hits[mod_id].add(term)
                covered_terms[mod_id].add(term)
                matched_fields[mod_id].update(fields)

        results: List[SearchResult] = []
        for mod_id, score in scores.items():
            document = self._docs.get(mod_id)
            if document is None:
                continue

            coverage = len(covered_terms[mod_id]) / len(wanted)
            if coverage < 0.5 and len(wanted) > 1:
                # Require most query terms to appear somewhere.
                continue

            # Popularity prior: at most +15 % so it only breaks ties.
            prior = 1.0 + 0.15 * _saturate(document.mod.users)
            results.append(
                SearchResult(
                    mod=document.mod,
                    score=score * (0.6 + 0.4 * coverage) * prior,
                    matched_fields=tuple(
                        sorted(matched_fields[mod_id], key=lambda f: -FIELD_WEIGHTS.get(f, 0))
                    ),
                    snippet=_snippet(document.mod, wanted),
                )
            )

        return results

    # -- suggestions ------------------------------------------------------ #
    def suggest(self, prefix: str, *, limit: int = 5) -> List[str]:
        """Return index terms starting with *prefix*, most popular first."""

        from .validation import sanitize_query

        cleaned = sanitize_query(prefix).casefold()
        if not cleaned:
            return []

        with self._lock:
            candidates = [term for term in self._postings if term.startswith(cleaned)]

        candidates.sort(key=lambda term: (len(term), term))
        return candidates[: max(1, int(limit))]

    def did_you_mean(self, query: str, *, limit: int = 3, max_distance: int = 2) -> List[str]:
        """Suggest corrections for possibly misspelled query terms."""

        from .validation import sanitize_query

        terms = tokenize(sanitize_query(query))
        if not terms:
            return []

        with self._lock:
            vocabulary = list(self._postings)

        suggestions: List[Tuple[int, str]] = []
        for term in terms:
            best: Optional[Tuple[int, str]] = None
            for candidate in vocabulary:
                if abs(len(candidate) - len(term)) > max_distance:
                    continue
                distance = _edit_distance(term, candidate, max_distance)
                if distance <= max_distance and (best is None or distance < best[0]):
                    best = (distance, candidate)
            if best and best[1] != term:
                suggestions.append(best)

        seen: Set[str] = set()
        unique: List[str] = []
        for _, candidate in sorted(suggestions):
            if candidate not in seen:
                seen.add(candidate)
                unique.append(candidate)

        return unique[: max(1, int(limit))]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def mod_id_key(mod: Mod) -> str:
    return mod.id


def _saturate(value: float, *, scale: float = 25000.0) -> float:
    """Squash a popularity count into ``[0, 1]``."""

    if value <= 0:
        return 0.0
    return value / (value + scale)


def _snippet(mod: Mod, wanted: Set[str], *, width: int = 160) -> Optional[str]:
    """Build a short description snippet with the matched terms bolded."""

    text = mod.description or mod.name
    if not text:
        return None

    lowered = text.casefold()
    position = -1
    matched = ""

    for term in sorted(wanted, key=len, reverse=True):
        index = lowered.find(term)
        if index >= 0 and (position < 0 or index < position):
            position, matched = index, term

    start = 0 if position < 0 else max(0, position - width // 3)

    snippet = text[start : start + width]
    if start > 0:
        snippet = "…" + snippet
    if start + width < len(text):
        snippet += "…"

    if matched:
        pattern = re.compile(re.escape(matched), re.IGNORECASE)
        snippet = pattern.sub(lambda m: f"**{m.group(0)}**", snippet, count=1)

    return snippet.strip() or None


def _edit_distance(a: str, b: str, max_distance: int) -> int:
    """Bounded Levenshtein distance (returns ``max_distance + 1`` when above)."""

    if a == b:
        return 0

    len_a, len_b = len(a), len(b)
    if abs(len_a - len_b) > max_distance:
        return max_distance + 1

    previous = list(range(len_b + 1))

    for i in range(1, len_a + 1):
        current = [i] + [0] * len_b
        row_min = current[0]
        cost_row = a[i - 1]

        for j in range(1, len_b + 1):
            cost = 0 if cost_row == b[j - 1] else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            row_min = min(row_min, current[j])

        if row_min > max_distance:
            return max_distance + 1

        previous = current

    return previous[len_b]


def build_index(catalog: Catalog) -> ModIndex:
    """Convenience factory: ``build_index(client.get_catalog())``."""

    return ModIndex(catalog)


def search_catalog(
    catalog: Catalog,
    query: str,
    *,
    limit: int = 20,
    filters: Optional[SearchFilters] = None,
) -> List[SearchResult]:
    """One-shot search over *catalog* (builds a throwaway index)."""

    return ModIndex(catalog).search(query, limit=limit, filters=filters)


def _unused(_: Iterable[str]) -> None:  # pragma: no cover - keeps linters calm
    return None
