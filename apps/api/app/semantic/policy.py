"""Sensitive-field classification (``semantic-layer/policies/*.yaml``).

``forbid`` fields are never exposed, indexed, sent to a model or returned.
``mask`` fields may only appear if the semantic layer marks them ``sensitive``;
the query layer (Phase 4) never projects them raw.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.semantic.models import Classification, SensitivePattern

# Built-in floor that applies even if a deployment ships an empty policy file.
BUILTIN_PATTERNS: tuple[SensitivePattern, ...] = (
    SensitivePattern(
        pattern=r"(?i)(password|passwd|secret|api_?key|token|private_?key)",
        classification=Classification.RESTRICTED,
        action="forbid",
        description="Credentials",
    ),
    SensitivePattern(
        pattern=r"(?i)(bank_?account|iban|swift|routing_?number|card_?number|cvv|pan\b)",
        classification=Classification.RESTRICTED,
        action="forbid",
        description="Financial account identifiers",
    ),
    SensitivePattern(
        pattern=r"(?i)(ssn|social_?security|national_?id|passport|tax_?id)",
        classification=Classification.RESTRICTED,
        action="forbid",
        description="Government identifiers",
    ),
    # India: GST registration number, PAN, Aadhaar - not GST amounts or rates. Delimited
    # by `_`/edges because `\b` treats `_` as a word character (so `pan\b` above misses
    # `pan_no`) and bare substrings would hit ordinary words ("company", "panel").
    SensitivePattern(
        pattern=(
            r"(?i)^gst$|(^|_)(gstin|gst_?(no|num|number|id|reg(istration)?(_?no)?)"
            r"|pan_?(no|num|number|card)|aadhaa?r(_?(no|number))?)(_|$)"
        ),
        classification=Classification.RESTRICTED,
        action="forbid",
        description="Government identifiers (India)",
    ),
)


@dataclass(frozen=True, slots=True)
class FieldClassification:
    classification: Classification
    action: str
    pattern: str


class SensitiveFieldPolicy:
    def __init__(self, patterns: list[SensitivePattern] | tuple[SensitivePattern, ...]) -> None:
        combined = list(BUILTIN_PATTERNS) + [p for p in patterns if p not in BUILTIN_PATTERNS]
        self._rules = [(re.compile(p.pattern), p) for p in combined]

    def classify(self, field_name: str) -> FieldClassification | None:
        """Most restrictive matching rule wins (``forbid`` beats ``mask``)."""
        matches = [p for rx, p in self._rules if rx.search(field_name)]
        if not matches:
            return None
        best = min(
            matches,
            key=lambda p: (p.action != "forbid", p.classification != Classification.RESTRICTED),
        )
        return FieldClassification(best.classification, best.action, best.pattern)

    def is_forbidden(self, field_name: str) -> bool:
        result = self.classify(field_name)
        return result is not None and result.action == "forbid"
