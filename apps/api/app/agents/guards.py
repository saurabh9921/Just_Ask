"""Deterministic question guards, applied before any model sees the question.

The plan validator already makes it impossible to *return* forbidden data, but a
model can still answer a different question than the one asked (for example drop
"tax IDs" and return customer counts). These guards refuse such requests
explicitly and consistently, independent of which model is configured:

* write requests - SQL statement shapes (``DELETE FROM``, ``DROP TABLE`` ...) anywhere
  in the text, or an imperative write verb at the start;
* sensitive-data requests - question terms (words and word pairs, singularised) that
  *entirely* match a ``forbid`` rule of the sensitive-field policy, or that name a
  field the semantic layer lists in ``forbidden_fields``.

Terms are matched whole so that e.g. ``pan`` (card PAN) does not fire on "Japan".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise

from app.query.plans import RefusalReason
from app.semantic.loader import SemanticBundle
from app.semantic.models import Classification

_SQL_WRITE = re.compile(
    r"(?ix)"
    r"\b(delete\s+from|insert\s+into|merge\s+into|truncate\s+table"
    r"|drop\s+(table|view|database|schema|index|user|role)"
    r"|alter\s+(table|view|database|schema|user|role)"
    r"|create\s+(table|view|procedure|function|index|user|role|database|schema)"
    r"|update\s+[\w.\"`\[\]]+\s+set"
    r"|grant\s+\w+(\s*,\s*\w+)*\s+(on|to)"
    r"|revoke\s+\w+(\s*,\s*\w+)*\s+(on|from)"
    r"|exec(ute)?\s+(sp_|xp_|proc(edure)?\b))"
)
_IMPERATIVE_WRITE = re.compile(
    r"(?i)^\s*(please\s+|can\s+you\s+|could\s+you\s+)?"
    r"(delete|drop|truncate|insert|alter|grant|revoke|wipe|purge)\b"
)
_WORD = re.compile(r"[a-z][a-z0-9]*")


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    reason: RefusalReason
    rule: str  # safe to log/audit: a rule name, never the question text


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 2 and word.endswith("s") and not word.endswith("ss") else word


def question_terms(question: str) -> set[str]:
    words = [_singular(w) for w in _WORD.findall(question.lower())]
    terms = set(words)
    for a, b in pairwise(words):
        terms.add(f"{a}_{b}")
        terms.add(f"{a}{b}")
    return terms


class QuestionGuard:
    def __init__(self, bundle: SemanticBundle) -> None:
        self._policy = bundle.policy
        self._forbidden = {
            f.lower() for e in bundle.entities.values() for f in e.security.forbidden_fields
        }

    def check(self, question: str) -> GuardVerdict | None:
        if _SQL_WRITE.search(question) or _IMPERATIVE_WRITE.search(question):
            return GuardVerdict(RefusalReason.WRITE_NOT_ALLOWED, "write_request")
        for term in sorted(question_terms(question)):
            verdict = self._sensitive(term)
            if verdict is not None:
                return verdict
        return None

    def _sensitive(self, term: str) -> GuardVerdict | None:
        rule = self._policy.classify(term)
        whole = rule is not None and re.fullmatch(rule.pattern, term) is not None
        if whole and rule is not None and rule.action == "forbid":
            return GuardVerdict(RefusalReason.FORBIDDEN_FIELD, "forbidden_term")
        if term in self._forbidden:
            restricted = rule is not None and rule.classification == Classification.RESTRICTED
            reason = RefusalReason.FORBIDDEN_FIELD if restricted else RefusalReason.SENSITIVE_FIELD
            return GuardVerdict(reason, "forbidden_field")
        return None
