"""Business roles of semantic-layer dimensions.

A field's role decides what the pipeline may do with it *automatically*:

* ``category``   - a small set of business values (status, stage, source, region):
  breakdowns and charts.
* ``name``       - names a business entity (``company_name``): entity resolution,
  filters, rankings.
* ``identifier`` - keys and codes (``id``, ``*_id``): filters and joins only; never a
  chart axis.
* ``free_text``  - notes, comments, descriptions: never grouped or charted unless the
  user names the field explicitly, and even then not charted.

An explicit ``role`` in the semantic layer wins; otherwise it is inferred from the
field's name - a field does not become a chart dimension just because it exists.
"""

from __future__ import annotations

import re
from typing import Final, Literal

from app.semantic.models import Dimension, DimensionRole

ResultRole = Literal["category", "name", "identifier", "free_text", "time"]

_IDENTIFIER: Final = re.compile(r"(^id$|_id$|_ids$|^uuid$|_uuid$|^guid$|_guid$|_key$)", re.I)
_NAME: Final = re.compile(r"(^name$|_name$|^title$)", re.I)
_FREE_TEXT_TOKENS: Final = frozenset(
    {
        "note",
        "notes",
        "comment",
        "comments",
        "remark",
        "remarks",
        "description",
        "desc",
        "summary",
        "message",
        "messages",
        "body",
        "text",
        "details",
        "narrative",
        "explanation",
        "url",
        "link",
        "website",
        "address",
        "html",
        "json",
        "requirement",
        "requirements",
        "feedback",
        "instructions",
    }
)
# Fields whose values describe where a record stands in a process.
STATUS_TOKENS: Final = frozenset(
    {"status", "stage", "phase", "verdict", "decision", "outcome", "tier", "priority", "funnel"}
)


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t}


def infer_role(name: str) -> DimensionRole:
    """Role from a field name: ``analysis_note`` → free_text, ``companies_id`` → identifier."""
    if _IDENTIFIER.search(name):
        return "identifier"
    if _tokens(name) & _FREE_TEXT_TOKENS:
        return "free_text"
    if _NAME.search(name):
        return "name"
    return "category"


def dimension_role(dim: Dimension) -> DimensionRole:
    if dim.role is not None:
        return dim.role
    # The physical column can reveal what a friendly name hides (``ref`` → ``order_id``).
    roles = {infer_role(dim.name), infer_role(dim.field)}
    if "identifier" in roles:
        return "identifier"
    if "free_text" in roles:
        return "free_text"
    if "name" in roles:
        return "name"
    return "category"


def is_status_like(dim: Dimension) -> bool:
    return dimension_role(dim) == "category" and bool(_tokens(dim.name) & STATUS_TOKENS)
