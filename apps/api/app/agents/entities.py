"""Entity resolution: map a business name in the question to governed records.

"lead analysis for pending metals co" names a company. Before any data is queried the
name is resolved against the semantic layer's *name* fields (``role: name``): those of
master entities reachable from the subject through an approved many-to-one join
(``Companies.name`` via ``Leads.companies_id``) first, then the subject's own
(``Leads.company_name``):

1. exact match - case-insensitive, trimmed;
2. safe normalized match - punctuation, "&"/"and" and legal suffixes (Ltd, Pvt, Co,
   LLP...) ignored, and only when no other similar name exists;
3. anything else is only a *suggestion* and is never applied automatically.

One record → its key becomes a filter (``companies_id in [2]``). Several records with
the name, several candidates, or none → the user is asked; the question is never
silently answered for a different company - or for every company.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from app.query.plans import FilterOp, QueryFilter, QueryPlan
from app.rag.text import tokenize
from app.semantic.loader import SemanticBundle
from app.semantic.models import Entity
from app.semantic.roles import dimension_role

LEGAL_TOKENS: Final = frozenset(
    {
        "co",
        "company",
        "corp",
        "corporation",
        "inc",
        "incorporated",
        "ltd",
        "limited",
        "pvt",
        "private",
        "llp",
        "llc",
        "plc",
        "gmbh",
        "pte",
        "the",
    }
)
MAX_SUGGESTIONS: Final = 6
# Master entities that name a business party come first: a campaign or a lead can be
# named after a company, and "lead analysis for X" means the company X.
_PARTY: Final = frozenset(
    {
        "company",
        "customer",
        "client",
        "supplier",
        "vendor",
        "account",
        "organization",
        "organisation",
        "firm",
        "business",
        "party",
        "seller",
        "buyer",
        "dealer",
        "distributor",
        "partner",
        "merchant",
    }
)
# Straight and typographic quotes (built from code points to keep the source ASCII).
_QUOTES: Final = "'\"" + "".join(map(chr, (0x201C, 0x201D, 0x2018, 0x2019)))
MAX_TARGETS: Final = 4

# A name runs from the cue to one of these words (or the end of the question).
_STOP: Final = (
    r"by|in|during|since|between|from|to|this|last|next|over|per|where|vs|versus|compared"
    r"|grouped|sorted|order|ordered|top|for|about|with|having|and show|that|which|who"
)
_CUE: Final = re.compile(
    r"\b(?:for|about|regarding|(?<=analysis )of|(?<=report )of|(?<=overview )of|"
    r"(?<=summary )of|(?<=insights )of)\s+(?:the\s+)?"
    r"(?:(?P<kind>company|customer|client|supplier|vendor|account|firm|seller|buyer)\s+)?"
    rf"(?P<name>.+?)\s*(?=$|[?!]|\.\s|\.$|,|;|\b(?:{_STOP})\b)",
    re.IGNORECASE,
)
_BY_ID: Final = re.compile(
    r"\b(?P<kind>company|customer|client|supplier|vendor|account)\s+(?:id\s*)?#?(?P<id>\d{1,12})\b",
    re.IGNORECASE,
)
_TIME_WORDS: Final = frozenset(
    {
        "last",
        "this",
        "next",
        "previous",
        "current",
        "past",
        "day",
        "days",
        "week",
        "weeks",
        "month",
        "months",
        "quarter",
        "quarters",
        "year",
        "years",
        "today",
        "yesterday",
        "ytd",
        "mtd",
        "fy",
        "q1",
        "q2",
        "q3",
        "q4",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "jan",
        "feb",
        "mar",
        "apr",
        "jun",
        "jul",
        "aug",
        "sep",
        "sept",
        "oct",
        "nov",
        "dec",
        "date",
        "period",
        "time",
        # "the last six months"
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "fifteen",
        "twenty",
        "thirty",
        "sixty",
        "ninety",
        "hundred",
        "few",
        "several",
    }
)
_GENERIC_START: Final = frozenset(
    {"each", "all", "every", "any", "overall", "total", "my", "our", "me", "us", "them", "it"}
)


@dataclass(frozen=True, slots=True)
class Mention:
    text: str  # the name as written ("pending metals co")
    start: int
    end: int
    kind: str | None = None  # "company" when the user said so
    by_id: int | None = None  # "company 1050769"


@dataclass(frozen=True, slots=True)
class NameTarget:
    """Where a name can be looked up and how the subject is then filtered."""

    entity: str  # entity holding the names (Companies, or the subject itself)
    name_dim: str  # its name-role dimension
    key_dim: str | None  # its key dimension (id) - for master entities
    filter_dim: str  # the subject's dimension to filter on (companies_id / company_name)
    via_key: bool
    noun: str  # "company"


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    key: Any | None
    level: Literal["exact", "normalized", "partial"]
    target: NameTarget


@dataclass
class Resolution:
    mention: Mention
    status: Literal["resolved", "ambiguous", "suggestions", "not_found"]
    noun: str
    match: Literal["exact", "normalized", "id", "ambiguous", "suggestions", "not_found"]
    chosen: list[Candidate] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    filter: QueryFilter | None = None
    filter_dim: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved"

    def clarification(self) -> str | None:
        name = self.mention.text
        if self.status == "resolved":
            return None
        if self.status == "not_found":
            return (
                f"I couldn't find a {self.noun} named “{name}”. "
                "Check the spelling, or try part of the name."
            )
        listed = "; ".join(_describe(c) for c in self.candidates[:MAX_SUGGESTIONS])
        if self.status == "ambiguous":
            return (
                f"Several {_plural(self.noun)} match “{name}”: {listed}. Which one do you mean? "
                f"You can name it exactly or say “{self.noun} <id>”."
            )
        return f"I couldn't find a {self.noun} named exactly “{name}”. Did you mean: {listed}?"


RowRunner = Callable[[QueryPlan, str], Awaitable[Sequence[Sequence[Any]]]]


# ------------------------------------------------------------------ mentions
def find_mention(question: str, vocabulary_tokens: frozenset[str] = frozenset()) -> Mention | None:
    """The business name the question is about, if any ("... for <name>")."""
    by_id = _BY_ID.search(question)
    if by_id is not None:
        return Mention(
            text=by_id.group(0),
            start=by_id.start(),
            end=by_id.end(),
            kind=by_id.group("kind").lower(),
            by_id=int(by_id.group("id")),
        )
    for match in _CUE.finditer(question):
        name = _trim_vocabulary(
            match.group("name").strip().strip(_QUOTES).strip(), vocabulary_tokens
        )
        if _plausible_name(name, vocabulary_tokens):
            kind = match.group("kind")
            return Mention(
                text=name,
                start=match.start("name"),
                end=match.end("name"),
                kind=kind.lower() if kind else None,
            )
    return None


def _trim_vocabulary(name: str, vocabulary_tokens: frozenset[str]) -> str:
    """'North region' → 'North': a trailing field word says what the name is."""
    words = name.split()
    while len(words) > 1 and set(tokenize(words[-1])) <= vocabulary_tokens:
        words.pop()
    return " ".join(words)


def _plausible_name(name: str, vocabulary_tokens: frozenset[str]) -> bool:
    words = [w for w in re.split(r"[^a-z0-9&]+", name.lower()) if w]
    if not words or len(name) > 120:
        return False
    if words[0] in _GENERIC_START:
        return False
    if all(w in _TIME_WORDS or w.isdigit() for w in words):
        return False
    significant = [w for w in words if w not in LEGAL_TOKENS]
    if not significant:
        return False
    # "for each region", "for sales": only governed vocabulary, not a name.
    return not set(tokenize(name)) <= vocabulary_tokens


# ------------------------------------------------------------------- names
def normalize_name(text: str) -> str:
    """Comparable form: lower-case, '&' → 'and', no punctuation or legal suffixes."""
    lowered = text.lower().replace("&", " and ")
    words = [w for w in re.split(r"[^a-z0-9]+", lowered) if w and w not in LEGAL_TOKENS]
    return " ".join(words)


def match_level(candidate: str, mention: str) -> Literal["exact", "normalized", "partial"]:
    if candidate.strip().lower() == mention.strip().lower():
        return "exact"
    if normalize_name(candidate) and normalize_name(candidate) == normalize_name(mention):
        return "normalized"
    return "partial"


def search_terms(mention: str) -> list[str]:
    """Substrings to search for: the name as typed, then its first significant words."""
    full = " ".join(mention.lower().split())
    words = normalize_name(mention).split()
    terms = [full]
    for n in (2, 1):
        core = " ".join(words[:n])
        if len(core) >= 3 and core not in terms and len(words) >= n:
            terms.append(core)
            break
    return terms


# ----------------------------------------------------------------- targets
def name_targets(
    bundle: SemanticBundle, subject: Entity, usable: frozenset[str], kind: str | None = None
) -> list[NameTarget]:
    """Name fields to resolve against: related master entities first, then the subject's."""
    masters: list[NameTarget] = []
    for join in bundle.joins:
        if (
            join.from_entity != subject.entity
            or join.relationship not in {"many_to_one", "one_to_one"}
            or join.to_entity not in usable
            or len(join.on) != 1
        ):
            continue
        parent = bundle.entities[join.to_entity]
        fk = next((d for d in subject.dimensions if d.field == join.on[0].from_field), None)
        key = next((d for d in parent.dimensions if d.field == join.on[0].to_field), None)
        if fk is None or key is None:
            continue
        for dim in parent.dimensions:
            if dimension_role(dim) == "name" and not dim.sensitive:
                masters.append(
                    NameTarget(
                        entity=parent.entity,
                        name_dim=dim.name,
                        key_dim=key.name,
                        filter_dim=fk.name,
                        via_key=True,
                        noun=singular_noun(parent.entity),
                    )
                )
    own = [
        NameTarget(
            entity=subject.entity,
            name_dim=dim.name,
            key_dim=None,
            filter_dim=dim.name,
            via_key=False,
            noun=_noun_from_field(dim.name, subject.entity),
        )
        for dim in subject.dimensions
        if dimension_role(dim) == "name" and not dim.sensitive
    ]
    masters.sort(key=lambda t: t.noun.split()[-1] not in _PARTY)  # stable: joins' order
    targets = masters + own
    if kind is not None:  # "for company X": look where companies live first
        targets.sort(key=lambda t: t.noun != kind)
    return targets[:MAX_TARGETS]


# -------------------------------------------------------------- resolution
async def resolve_mention(
    mention: Mention,
    targets: Sequence[NameTarget],
    run: RowRunner,
    *,
    max_candidates: int = 50,
) -> Resolution:
    noun = mention.kind or (targets[0].noun if targets else "record")
    if not targets:
        return Resolution(mention, "not_found", noun, "not_found")
    if mention.by_id is not None:
        return await _resolve_id(mention, targets, run, noun)

    partial: list[Candidate] = []
    for target in targets:
        found = await _lookup(mention, target, run, max_candidates)
        exact = [c for c in found if c.level == "exact"]
        normalized = [c for c in found if c.level == "normalized"]
        others = [c for c in found if c.level == "partial"]
        if exact:
            return _decide(mention, target, exact, "exact", others)
        if normalized:
            return _decide(mention, target, normalized, "normalized", others)
        partial += others
    suggestions = _unique_names(partial)
    if suggestions:
        return Resolution(
            mention, "suggestions", targets[0].noun, "suggestions", candidates=suggestions
        )
    return Resolution(mention, "not_found", noun, "not_found")


async def _lookup(
    mention: Mention, target: NameTarget, run: RowRunner, limit: int
) -> list[Candidate]:
    dims = [target.key_dim, target.name_dim] if target.key_dim else [target.name_dim]
    seen: dict[tuple[Any, str], Candidate] = {}
    for term in search_terms(mention.text):
        plan = QueryPlan(
            entity=target.entity,
            dimensions=dims,
            filters=[QueryFilter(field=target.name_dim, op=FilterOp.CONTAINS, value=term)],
            limit=limit,
        )
        rows = await run(plan, f"resolve {target.noun}: {target.entity}.{target.name_dim}")
        for row in rows:
            key, name = (row[0], row[1]) if target.key_dim else (None, row[0])
            if not isinstance(name, str) or not name.strip():
                continue
            level = match_level(name, mention.text)
            seen.setdefault((key, name), Candidate(name, key, level, target))
        if seen:  # names containing the whole mention: no need for looser terms
            break
    return list(seen.values())


def _decide(
    mention: Mention,
    target: NameTarget,
    best: list[Candidate],
    level: Literal["exact", "normalized"],
    others: list[Candidate],
) -> Resolution:
    distinct = _unique(best)
    # A normalized match is only safe when nothing else looks like the name.
    if len(distinct) == 1 and (level == "exact" or not others):
        chosen = distinct[0]
        return Resolution(
            mention,
            "resolved",
            target.noun,
            level,
            chosen=[chosen],
            candidates=distinct,
            filter=QueryFilter(
                field=target.filter_dim, op=FilterOp.IN, value=[filter_value(chosen)]
            ),
            filter_dim=target.filter_dim,
        )
    return Resolution(
        mention,
        "ambiguous",
        target.noun,
        "ambiguous",
        candidates=distinct + _unique(others),
    )


async def _resolve_id(
    mention: Mention, targets: Sequence[NameTarget], run: RowRunner, noun: str
) -> Resolution:
    for target in targets:
        if not target.key_dim or (mention.kind and target.noun != mention.kind):
            continue
        plan = QueryPlan(
            entity=target.entity,
            dimensions=[target.key_dim, target.name_dim],
            filters=[QueryFilter(field=target.key_dim, op=FilterOp.EQ, value=mention.by_id)],
            limit=2,
        )
        rows = await run(plan, f"resolve {target.noun} by id")
        if rows:
            chosen = Candidate(str(rows[0][1]), rows[0][0], "exact", target)
            return Resolution(
                mention,
                "resolved",
                target.noun,
                "id",
                chosen=[chosen],
                candidates=[chosen],
                filter=QueryFilter(
                    field=target.filter_dim, op=FilterOp.IN, value=[filter_value(chosen)]
                ),
                filter_dim=target.filter_dim,
            )
    return Resolution(mention, "not_found", noun, "not_found")


# ------------------------------------------------------------------ helpers
def singular_noun(entity_name: str) -> str:
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", entity_name).lower().split()
    if not words:
        return "record"
    words[-1] = singular(words[-1])
    return " ".join(words)


def plural_noun(entity_name: str) -> str:
    """Leads → leads, Invoice → invoices, LeadCandidates → lead candidates."""
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", entity_name).lower().split()
    if not words:
        return "records"
    words[-1] = plural(words[-1])
    return " ".join(words)


def plural(word: str) -> str:
    """invoice → invoices, company → companies, status → statuses; leads stays."""
    if singular(word) != word:  # already plural
        return word
    if word.endswith("y") and len(word) > 2 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


def singular(word: str) -> str:
    """leads → lead, companies → company; status, analysis, address stay."""
    if word.endswith(("ss", "us", "is")) or len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "ches", "shes", "xes")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def _plural(noun: str) -> str:
    if noun.endswith("y") and noun[-2:-1] not in "aeiou":
        return noun[:-1] + "ies"
    if noun.endswith(("s", "x", "ch", "sh")):
        return noun + "es"
    return noun + "s"


def _noun_from_field(name: str, entity: str) -> str:
    stem = re.sub(r"_?(name|title)$", "", name).replace("_", " ").strip()
    return stem or singular_noun(entity)


def _unique(candidates: Sequence[Candidate]) -> list[Candidate]:
    seen: set[tuple[Any, str]] = set()
    out: list[Candidate] = []
    for c in candidates:
        key = (c.key if c.key is not None else c.name.lower(), c.name)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def filter_value(c: Candidate) -> str | int:
    """What the subject is filtered on: the master key, or the name itself."""
    if c.target.via_key and isinstance(c.key, int) and not isinstance(c.key, bool):
        return c.key
    if c.target.via_key and c.key is not None:
        return str(c.key)
    return c.name


def _unique_names(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Suggestions across targets: each master record once (two companies can share a
    name, so they stay apart by id), plus names found only on the subject, once."""
    keys: set[tuple[str, Any]] = set()
    names: set[str] = set()
    out: list[Candidate] = []
    for c in candidates:
        name = c.name.lower()
        if c.key is not None:
            if (c.target.entity, c.key) in keys:
                continue
            keys.add((c.target.entity, c.key))
        elif name in names:
            continue
        names.add(name)
        out.append(c)
    return out


def _describe(c: Candidate) -> str:
    return f"{c.name} (#{c.key})" if c.key is not None else c.name
