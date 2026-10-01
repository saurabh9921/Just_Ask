"""Text utilities shared by ingestion and retrieval.

* ``tokenize`` - ERP-aware lexical tokens. Business codes (``SO-100231``,
  ``WH-01``, ``PO#42``) are kept whole *and* split, snake/camel case is split, so
  sparse retrieval matches both ``sales_orders`` and "sales orders".
* ``sanitize_untrusted`` - neutralises instruction-like content in text that
  comes from customers' databases (column/table comments) or other untrusted
  sources before it is indexed and, later, placed into prompts.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

_CODE: Final = re.compile(r"\b[A-Za-z]{1,6}[-#/][A-Za-z0-9]{1,20}\b")
_WORD: Final = re.compile(r"[A-Za-z0-9]+")
_CAMEL: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_STOPWORDS: Final = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "show",
        "me",
        "the",
        "to",
        "what",
        "which",
        "with",
        "please",
        "give",
        "list",
    ]
)


def tokenize(text: str, *, keep_stopwords: bool = False) -> list[str]:
    """Deterministic lexical tokens (lower-cased)."""
    tokens: list[str] = [c.lower() for c in _CODE.findall(text)]
    for raw in _WORD.findall(text.replace("_", " ")):
        parts = _CAMEL.sub(" ", raw).split() or [raw]
        for part in parts:
            token = part.lower()
            if keep_stopwords or token not in _STOPWORDS:
                tokens.append(token)
    return tokens


# ---------------------------------------------------------------- sanitising
_INJECTION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (all|any|the)?\s*(previous|prior|above|earlier) (instructions|prompts?|rules)",
        r"disregard (all|any|the)?\s*(previous|prior|above) (instructions|rules)",
        r"you are now\b",
        r"\b(system|assistant|developer)\s*(prompt|message)?\s*:",
        r"</?\s*(system|assistant|user|instructions?)\s*>",
        r"\bact as\b",
        r"\bexecute\b.{0,40}\b(sql|query|command)\b",
        r"\b(drop|delete|truncate|alter|grant|insert|update)\s+(table|from|into|database|user)\b",
        r"reveal (your|the) (system )?(prompt|instructions|secrets?|password)",
    )
)
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FENCES: Final = re.compile(r"`{3,}|~{3,}")
FILTERED: Final = "[filtered]"


def sanitize_untrusted(text: str | None, *, max_length: int = 500) -> tuple[str, bool]:
    """Return ``(clean_text, flagged)``.

    Normalises Unicode (NFKC defeats look-alike tricks), strips control
    characters and code fences, replaces instruction-like phrases with
    ``[filtered]`` and truncates. ``flagged`` records that something was removed
    so the document can carry ``untrusted_flagged=true`` for auditing.
    """
    if not text:
        return "", False
    clean = unicodedata.normalize("NFKC", text)
    clean = _CONTROL.sub(" ", clean)
    flagged = False
    if _FENCES.search(clean):
        clean = _FENCES.sub(" ", clean)
        flagged = True
    for pattern in _INJECTION_PATTERNS:
        clean, count = pattern.subn(FILTERED, clean)
        flagged = flagged or count > 0
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) > max_length:
        clean = clean[: max_length - 1].rstrip() + "…"
    return clean, flagged
