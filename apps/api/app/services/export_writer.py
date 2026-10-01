"""Streaming CSV/JSON export writer with a sensitive-data scan.

Rows arrive in batches from ``DatabaseAdapter.stream_readonly`` and are written straight
to disk: memory use is bounded by one batch, whatever the export size.

Every text cell is scanned before it is written. The plan validator already keeps
forbidden and sensitive *fields* out of a query, but reporting views can still carry
restricted *values* in free-text columns; an export containing a payment-card number
(Luhn-valid) or an IBAN (mod-97-valid) is aborted with ``SENSITIVE_DATA_DETECTED``
instead of being made downloadable.

CSV cells that a spreadsheet would evaluate as a formula (leading ``= + - @`` or
tab/CR) are prefixed with ``'`` (CSV/formula-injection defence), matching the
extension's client-side export.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TextIO

from app.core.exceptions import AppError

ExportFormat = Literal["csv", "json"]

_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_IBAN_CANDIDATE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b")
_FORMULA_PREFIX = re.compile(r"^[=+\-@\t\r]")


class SensitiveDataDetectedError(AppError):
    status_code = 422
    code = "SENSITIVE_DATA_DETECTED"
    default_message = "The export contains restricted data (for example card or bank numbers)."


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _iban_valid(candidate: str) -> bool:
    iban = candidate.replace(" ", "")
    if not 15 <= len(iban) <= 34:
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(numeric) % 97 == 1


def sensitive_kind(text: str) -> str | None:
    """``"card_number"``, ``"iban"`` or ``None`` for one cell value."""
    for match in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits) and len(set(digits)) > 1:
            return "card_number"
    for match in _IBAN_CANDIDATE.finditer(text.upper()):
        if _iban_valid(match.group(0)):
            return "iban"
    return None


def csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, int | float):
        return str(value)
    text = str(value)
    return f"'{text}" if _FORMULA_PREFIX.match(text) else text


def json_cell(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


@dataclass
class WriteSummary:
    row_count: int
    byte_size: int
    sha256: str


class ExportWriter:
    """Write one export file incrementally. Use as a context manager."""

    def __init__(self, path: Path, fmt: ExportFormat, columns: list[tuple[str, str]]) -> None:
        self.path = path
        self.fmt = fmt
        self.columns = columns  # (name, label)
        self.rows = 0
        self._file: TextIO | None = None
        self._csv: Any = None

    def __enter__(self) -> ExportWriter:
        self._file = self.path.open("w", encoding="utf-8", newline="")
        if self.fmt == "csv":
            self._file.write("﻿")  # spreadsheets detect UTF-8
            self._csv = csv.writer(self._file, lineterminator="\r\n")
            self._csv.writerow([csv_cell(label) for _, label in self.columns])
        else:
            header = json.dumps([{"name": n, "label": label} for n, label in self.columns])
            self._file.write('{"columns": ' + header + ', "rows": [\n')
        return self

    def write_batch(self, rows: list[tuple[object, ...]]) -> None:
        assert self._file is not None  # noqa: S101 - used inside the context manager
        names = [name for name, _ in self.columns]
        for row in rows:
            for value in row:
                if isinstance(value, str):
                    kind = sensitive_kind(value)
                    if kind is not None:
                        raise SensitiveDataDetectedError(details={"kind": kind})
            if self.fmt == "csv":
                self._csv.writerow([csv_cell(v) for v in row])
            else:
                prefix = ",\n" if self.rows else ""
                record = {n: json_cell(v) for n, v in zip(names, row, strict=False)}
                self._file.write(prefix + json.dumps(record, ensure_ascii=False))
            self.rows += 1

    def finish(self, extra: dict[str, Any] | None = None) -> None:
        assert self._file is not None  # noqa: S101
        if self.fmt == "json":
            tail = {"row_count": self.rows, **(extra or {})}
            body = json.dumps(tail, ensure_ascii=False, default=str)
            self._file.write("\n], " + body[1:])

    def __exit__(self, *exc: object) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def summary(self) -> WriteSummary:
        digest = hashlib.sha256()
        size = 0
        with self.path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(io.DEFAULT_BUFFER_SIZE * 16), b""):
                digest.update(chunk)
                size += len(chunk)
        return WriteSummary(self.rows, size, digest.hexdigest())
