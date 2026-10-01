"""Map native column type names to :class:`CanonicalType`."""

from __future__ import annotations

import re
from typing import Final

from app.connectors.common.types import CanonicalType

# Ordered: first match wins. Patterns match the lower-cased native type name.
_RULES: Final[tuple[tuple[re.Pattern[str], CanonicalType], ...]] = tuple(
    (re.compile(pattern), canonical)
    for pattern, canonical in (
        (r"^(uuid|uniqueidentifier)\b", CanonicalType.UUID),
        (r"^(bool|boolean|bit)\b", CanonicalType.BOOLEAN),
        (
            r"^(tinyint|smallint|mediumint|int|integer|bigint|int2|int4|int8"
            r"|serial|bigserial|smallserial)\b",
            CanonicalType.INTEGER,
        ),
        (r"^(numeric|decimal|number|money|smallmoney|dec)\b", CanonicalType.DECIMAL),
        (
            r"^(real|float|float4|float8|double|double precision|binary_float|binary_double)\b",
            CanonicalType.FLOAT,
        ),
        (
            r"^(timestamp|datetime|datetime2|smalldatetime|datetimeoffset|timestamptz)\b",
            CanonicalType.TIMESTAMP,
        ),
        (r"^date\b", CanonicalType.DATE),
        (r"^(time|timetz)\b", CanonicalType.TIME),
        (r"^(json|jsonb)\b", CanonicalType.JSON),
        (
            r"^(bytea|blob|binary|varbinary|image|raw|longblob|mediumblob|tinyblob)\b",
            CanonicalType.BINARY,
        ),
        (
            r"^(char|character|varchar|character varying|nchar|nvarchar|text|ntext|clob|nclob"
            r"|varchar2|nvarchar2|string|citext|enum|tinytext|mediumtext|longtext|name|xml)\b",
            CanonicalType.STRING,
        ),
    )
)


def normalize_type(raw_type: str) -> CanonicalType:
    name = raw_type.strip().lower()
    name = re.sub(r"\s*\(.*$", "", name)  # drop length/precision: varchar(20) -> varchar
    for pattern, canonical in _RULES:
        if pattern.match(name):
            return canonical
    return CanonicalType.OTHER
