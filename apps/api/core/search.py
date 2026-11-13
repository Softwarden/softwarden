"""Global search: the query parser, the registry of searchable kinds and the response.

`GET /api/v1/search` (core/search_views.py) parses `q` once (`parse`), then asks every registered `Searcher` for the
kinds it serves. A searcher owns its SQL: catalog/search.py serves records (applications, components, integrations,
AI agents), vendors, people and attachments; contracts register their own searcher from money/ the same way
(`register` in the app's `ready()`), and appear in the response with no change here or in the view.

Matching is PostgreSQL full text with the `simple` configuration (no stemming, no stop words: names of products and
companies are matched as typed) over stored `search_vector` columns (catalog migration 0031). Every term must match
(AND), each one anywhere in the record or in what it is matched through (a vendor's name, a tag, an AI model); the last
term is a prefix (`payr` finds Payroll), so the palette can search as the user types. `q` is normalised to Unicode NFC
before it is split into words, and a word the `simple` parser finds no lexeme in (a lone `²`) is left out and listed in
the response's `ignored`, rather than emptying the query; a query with no searchable word is a field error. When nothing at all matches, the
searchers that can are asked for names similar to the whole query (trigram word similarity, for typos); the response
then says `fuzzy: true`.

Snippets are made by `ts_headline` with two private-use characters as the match markers (removed from the text first),
and returned as segments `[{"text", "match"}]`. The SPA renders each segment as text, so nothing in a record can become
markup. ts_headline drops anything its parser reads as an HTML tag, so a snippet can omit such text; it never adds any.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from django.db import connection

from .capabilities import has_capability
from .problems import CapabilityRequired, FieldErrors

QUERY_MAX = 200  # characters of `q`
TERMS_MAX = 8  # words used; the rest are ignored
LIMIT_DEFAULT = 10  # results per kind
LIMIT_MAX = 100
START, STOP = "", ""  # ts_headline's match markers: private-use characters, stripped from the text first
MARKERS = START + STOP
# Short fragments around the matches; at most two, joined by an ellipsis.
HEADLINE_OPTIONS = (
    f'StartSel={START}, StopSel={STOP}, MaxWords=24, MinWords=8, ShortWord=2, MaxFragments=2, FragmentDelimiter=" … "'
)
# A word is a run of letters and digits, which may join with . @ / + # - _ ' (an email, a host, power-bi, c#). Quotes
# and backslashes, which would end or escape a to_tsquery lexeme, are never part of one.
WORD = re.compile(r"[^\W_](?:[\w.@/+#-]*[^\W_])?")


@dataclass(frozen=True)
class Query:
    """A parsed `q`: the trimmed, NFC-normalised text and its words, lowercased. `ignored`: words the text search
    parser finds nothing in (see `searchable`)."""

    text: str
    terms: tuple[str, ...]
    ignored: tuple[str, ...] = ()

    def term(self, i: int, weight: str = "") -> str:
        """to_tsquery source for word `i`, a prefix if it is the last word, limited to `weight` (A: names) if given."""
        prefix = ":*" if i == len(self.terms) - 1 else (":" if weight else "")
        return f"'{self.terms[i]}'{prefix}{weight}"

    @property
    def any(self) -> str:
        """Any word (snippets highlight every word found)."""
        return " | ".join(self.term(i) for i in range(len(self.terms)))

    @property
    def all(self) -> str:
        """Every word (ranking)."""
        return " & ".join(self.term(i) for i in range(len(self.terms)))

    @property
    def lowered(self) -> str:
        """The whole query as one lowercased phrase, for the exact and starts-with name bonus and typo matching."""
        return " ".join(self.text.split()).lower()


def parse(q: str | None) -> Query:
    """The query, or field errors keyed `q`: missing, too long, or without a letter or digit. The text is normalised
    to NFC first, so a decomposed accent (e + U+0301) is the same word as the composed one the records hold."""
    text = unicodedata.normalize("NFC", q or "").strip()
    if not text:
        raise FieldErrors({"q": ["Enter something to search for."]})
    if len(text) > QUERY_MAX:
        raise FieldErrors({"q": [f"Use at most {QUERY_MAX} characters."]})
    terms: list[str] = []
    for word in WORD.findall(text.lower()):
        if word not in terms:
            terms.append(word)
    if not terms:
        raise FieldErrors({"q": ["Enter a word with at least one letter or digit."]})
    return Query(text=text, terms=tuple(terms[:TERMS_MAX]))


def searchable(query: Query) -> Query:
    """The query without the words PostgreSQL's `simple` parser finds no lexeme in (a lone `²`, for instance, which
    Python counts as a digit): such a word would otherwise match nothing and empty the whole AND query. They are
    reported in `ignored`, not dropped silently; if no word is left, a field error keyed `q`. One query."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT w FROM unnest(%s::text[]) WITH ORDINALITY AS t(w, n) "
            "WHERE numnode(to_tsquery('simple'::regconfig, quote_literal(w))) > 0 ORDER BY n",
            [list(query.terms)],
        )
        kept = tuple(row[0] for row in cursor.fetchall())
    ignored = tuple(t for t in query.terms if t not in kept)
    if not kept:
        raise FieldErrors({"q": [f"Nothing in {query.text!r} can be searched for; use letters or digits."]})
    return Query(text=query.text, terms=kept, ignored=ignored)


def segments(headline: str | None) -> list[dict[str, Any]]:
    """ts_headline's output (with START/STOP markers) as [{"text", "match"}], adjacent text merged, no empty parts.
    Plain text only: the caller renders it as text."""
    if not headline:
        return []
    out: list[dict[str, Any]] = []
    match = False
    for part in re.split(f"([{MARKERS}])", headline):
        if part == START:
            match = True
        elif part == STOP:
            match = False
        elif part:
            if out and out[-1]["match"] == match:
                out[-1]["text"] += part
            else:
                out.append({"text": part, "match": match})
    return out


# ---- results ----------------------------------------------------------------------------------------------------


@dataclass
class Hit:
    """One result. `path` is the record's route under /api/v1/ (and the SPA's, for kinds with pages); `parent` names
    what an attachment belongs to, in the same shape without snippet."""

    id: str
    kind: str
    name: str
    path: str
    score: float
    archived: bool = False
    lifecycle: str | None = None
    snippet: dict[str, Any] | None = None
    parent: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "path": self.path,
            "score": round(self.score, 4),
            "archived": self.archived,
            "lifecycle": self.lifecycle,
            "snippet": self.snippet,
            "parent": self.parent,
        }


@dataclass
class Group:
    kind: str
    count: int = 0  # every match of this kind, not only those returned
    results: list[Hit] = field(default_factory=list)


@dataclass(frozen=True)
class Context:
    org: Any
    query: Query
    kinds: tuple[str, ...]  # the kinds asked of this searcher, already checked for capabilities
    limit: int
    include_archived: bool


def snippet(field_name: str, label: str, headline: str | None) -> dict[str, Any] | None:
    parts = segments(headline)
    return {"field": field_name, "label": label, "segments": parts} if parts else None


class Searcher:
    """Base of a registered source of results. Subclasses set `kinds` (in the order their groups appear) and
    `capabilities` (the capability each kind needs; None: any member of any edition), and implement `search`, which
    returns a Group for every kind in `ctx.kinds` (empty ones too). `similar` is the typo fallback; the default has
    none."""

    kinds: ClassVar[tuple[str, ...]] = ()
    capabilities: ClassVar[Mapping[str, str | None]] = {}

    def search(self, ctx: Context) -> dict[str, Group]:
        raise NotImplementedError

    def similar(self, ctx: Context) -> dict[str, Group]:
        return {}


REGISTRY: list[Searcher] = []


def register(searcher: Searcher) -> None:
    """Add a searcher (an app's `ready()`); a kind may be served by one searcher only. Registering the same class
    again is ignored."""
    if any(type(s) is type(searcher) for s in REGISTRY):
        return
    taken = {kind for s in REGISTRY for kind in s.kinds}
    clash = taken & set(searcher.kinds)
    if clash:
        raise ValueError(f"Search kinds already registered: {', '.join(sorted(clash))}")
    REGISTRY.append(searcher)


def all_kinds() -> list[str]:
    return [kind for searcher in REGISTRY for kind in searcher.kinds]


def _needs(kind: str) -> str | None:
    for searcher in REGISTRY:
        if kind in searcher.kinds:
            return searcher.capabilities.get(kind)
    raise KeyError(kind)


def allowed_kinds(org) -> list[str]:
    """The kinds this organization's edition can search, in response order."""
    return [k for k in all_kinds() if (cap := _needs(k)) is None or has_capability(org, cap)]


def requested_kinds(org, raw: str | None) -> list[str]:
    """`kinds` (comma-separated) checked against the registry and the organization's capabilities. Unset: every kind
    the organization has. An unknown kind is a field error; a kind whose capability is missing a 403 naming it."""
    if raw is None or not raw.strip():
        return allowed_kinds(org)
    known = all_kinds()
    asked = [k.strip() for k in raw.split(",") if k.strip()]
    unknown = [k for k in asked if k not in known]
    if unknown:
        raise FieldErrors({"kinds": [f"Choose from {', '.join(known)}."]})
    for kind in asked:
        cap = _needs(kind)
        if cap is not None and not has_capability(org, cap):
            raise CapabilityRequired(cap)
    return [k for k in known if k in asked]


def run(org, query: Query, kinds: Iterable[str], limit: int, include_archived: bool) -> dict[str, Any]:
    """The response body: {query, fuzzy, groups: [{kind, count, results}]} with a group for every kind searched, in
    registry order. One query per searcher, plus one per searcher when nothing matched and typos are tried."""
    kinds = list(kinds)
    plan = [(s, tuple(k for k in s.kinds if k in kinds)) for s in REGISTRY]
    plan = [(s, ks) for s, ks in plan if ks]
    groups: dict[str, Group] = {}
    for searcher, ks in plan:
        groups.update(searcher.search(Context(org, query, ks, limit, include_archived)))
    fuzzy = False
    if not any(g.count for g in groups.values()) and len(query.lowered) >= 3:
        for searcher, ks in plan:
            found = searcher.similar(Context(org, query, ks, limit, include_archived))
            if any(g.count for g in found.values()):
                fuzzy = True
                groups.update(found)
    return {
        "query": query.text,
        "ignored": list(query.ignored),
        "fuzzy": fuzzy,
        "groups": [
            {
                "kind": kind,
                "count": groups.get(kind, Group(kind)).count,
                "results": [h.as_dict() for h in groups.get(kind, Group(kind)).results],
            }
            for kind in kinds
        ],
    }
