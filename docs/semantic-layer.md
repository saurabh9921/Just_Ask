# Semantic layer

The semantic layer (`semantic-layer/`) is the **only vocabulary the AI may use**.
It maps business language ("revenue by territory last quarter") to governed objects:
entities backed by approved reporting views, dimensions, measures, time fields,
derived metrics, approved joins, a business glossary and example questions. The
model plans in these terms only; it never sees raw tables or writes SQL.

Code: `apps/api/app/semantic/` (`models.py`, `loader.py`, `validator.py`, `policy.py`).

## Directory layout

| Path | Content |
|---|---|
| `entities/*.yaml` | One entity per file (top-level `entity:`) or `entities: [...]` |
| `metrics/*.yaml` | `metrics: [...]` derived metrics |
| `joins/*.yaml` | `joins: [...]` — the only joins the compiler may generate |
| `synonyms/*.yaml` | `terms: [...]` glossary, `abbreviations: [...]` |
| `examples/*.yaml` | `examples: [...]` question → plan sketches |
| `policies/*.yaml` | `sensitive_fields: [...]` classification rules |

Shipped content: `SalesOrder`, `SalesLine`, `OrderStatusDaily`, `Inventory`,
`InventoryMovement`, `Customer`, `Invoice`, `PurchaseOrder`; 10 metrics; 4 joins;
34 glossary terms; 24 ERP abbreviations; 42 examples.

## Entity format

```yaml
entity: SalesOrder                      # PascalCase
description: Customer sales orders ...  # rich text: used for retrieval
source:
  logical_name: analytics_sales_orders  # must be in allowed_database_objects
  allowed_database_objects: [analytics_sales_orders]
  schema_name: analytics                # optional; defaults to the connection schema
synonyms: [sales, orders, SO]
classification: internal                # public | internal | confidential | restricted
large_fact: true                        # queries must carry a date filter (Phase 4)
default_time_field: order_date
dimensions:
  - {name: region, field: region_name, type: string, synonyms: [territory, zone, area]}
measures:
  - {name: total_sales, field: net_amount, aggregation: sum, type: decimal, format: currency}
  - {name: order_count, field: order_id, aggregation: count_distinct, type: integer}
time_fields:
  - {name: order_date, field: order_date, type: date, default: true}
security:
  required_filters:
    - tenant_code                       # shorthand for {field: tenant_code, value_from: tenant.slug}
  forbidden_fields: [bank_account, tax_id, payment_token]
  required_permissions: [analytics.query]   # default
formatting: {currency: USD, locale: en-US}
```

Rules enforced by the models (all with `extra="forbid"` — unknown keys are errors):

- Names: semantic names `snake_case`, entities `PascalCase`, physical names plain
  identifiers only (no dots, quotes, spaces or SQL).
- `sum`/`avg` only on numeric types; no duplicate field names within an entity.
- `forbidden_fields` can never be exposed as a dimension, measure or time field.
- `required_filters.value_from` is `tenant.slug`, `tenant.id` or
  `tenant.setting:<key>` — never literal SQL. The shipped layer filters every entity
  on `tenant_code = <tenant slug>`.

## Metrics

Structured expressions over measures (or other metrics) of the same entity — no SQL:

```yaml
metrics:
  - name: average_order_value
    entity: SalesOrder
    description: Net sales divided by the number of orders.
    expression: {op: divide, left: total_sales, right: order_count}   # add|subtract|multiply|divide|percent_of
    format: currency
    synonyms: [AOV]
```

Operands must exist; circular definitions are rejected.

## Joins

```yaml
joins:
  - name: sales_line_to_order
    from: SalesLine
    to: SalesOrder
    relationship: many_to_one
    "on":                                # quote it: YAML 1.1 reads a bare `on:` as `true`
      - {from_field: order_id, to_field: order_id}
```

Both entities must exist, self-joins are rejected, and join keys may not be forbidden fields.

## Glossary and abbreviations

`terms[].maps_to` points at an entity plus a measure, dimension or metric; the
validator checks the target exists. Abbreviations (`SO`, `PO`, `AR`, `SKU`, `AOV`,
`YTD`, `MoM`, …) expand ERP shorthand before retrieval.

## Examples

Each example carries a `question`, an `intent` (`aggregation`, `ranking`,
`comparison`, `trend`, `detail`, `explanation`, `clarification`), the entity,
dimensions/measures/time field (validated against the entity) and a `plan` sketch.
Examples are indexed in the `query_examples` collection and retrieved as few-shot
context for query planning (Phase 4). The evaluation suite (Phase 7) deliberately
uses *held-out* questions (`apps/api/tests/fixtures/eval-questions.yaml` for the test
fixture; your own file for live data), never these examples. Requests for forbidden data (bank accounts, tax IDs, payment
tokens, contact details) and write requests are included with intent
`clarification`, `plan.refuse: true` and a `notes` line starting "MUST BE REFUSED".

## Sensitive-field policy

`policies/sensitive-fields.yaml` classifies field names by regex:

- `forbid` — never exposed, indexed, sent to a model or returned (bank/IBAN/card,
  tax/government IDs, credentials/tokens, salary).
- `mask` — allowed only on dimensions marked `sensitive: true`; never projected raw
  (email, phone, address, contact name, date of birth).

Built-in floor rules (credentials, account numbers, government IDs) apply even if the
file is empty. The same policy screens discovered database metadata before indexing.

## Validation and binding

`load_semantic_layer(path)` parses YAML with `safe_load` (1 MB per file cap),
validates every model, then cross-checks references (metrics, joins, glossary,
examples, policy). Any error aborts loading with a list of issues. The bundle has a
SHA-256 `content_hash` used to version ingestion.

`bind_to_catalog(bundle, objects, default_schema)` checks the bundle against a
connection's discovered metadata: the source view must exist and every referenced
column (including `required_filters`) must be present with a compatible type.
Unbound entities are excluded from retrieval for that connection, so the model never
sees vocabulary the database cannot answer.

## Onboarding a live database

The product's `semantic-layer/` ships empty (only the sensitive-field policy). Build it
from the live database's own structure:

```bash
python scripts/dev.py onboard --tenant <t> --email <admin> connect --name "ERP" --type postgresql \
    --host <db-host> --database <db> --schema <schema> --db-user <read-only user>
python scripts/dev.py onboard --tenant <t> --email <admin> draft --name "ERP" --list
python scripts/dev.py onboard --tenant <t> --email <admin> draft --name "ERP" --objects "sales_*,customers"
#   review semantic-layer/entities/*.yaml and joins/*-draft-joins.yaml
python scripts/dev.py onboard --tenant <t> --email <admin> ingest --name "ERP"
```

`draft` (`app/semantic/drafting.py`) is deterministic and safe by default:

| Column | Becomes |
|---|---|
| matches the sensitive-field policy (`forbid` or `mask`) | never exposed; listed in `security.forbidden_fields` |
| `date` / `timestamp` | time field (a date-like name such as `order_date`/`posting_date` becomes the default) |
| text, boolean, uuid; key-like integers (`*_id`, `*_code`, `*_no`, `CustomerID`, PK/FK) | dimension |
| other numbers | `sum` measure (`currency` format for money-like names) + one `record_count` per entity |
| JSON, binary, unknown types, names that are not plain identifiers | skipped (listed in the file header) |

Objects above one million estimated rows with a time field are marked `large_fact`
(questions without a date range get the default 90-day window). Foreign keys between
selected objects become `many_to_one` join drafts. No row filter is added: add
`security.required_filters` if one database holds several companies' data. Drafts are
validated together with the existing layer before anything is written, and existing
files are never overwritten without `--force`.

**Review every draft** before ingesting: entity and field names in business language,
a description of what one row represents, synonyms users say ("turnover", "client"),
formats, and measures that must not be summed (unit prices, percentages → `avg`, or
delete them). Then add glossary terms, derived metrics and question → plan examples
for the questions your users ask most; they are the strongest lever on answer quality.
Prefer reporting views over raw transactional tables for large ERPs.

The test suite's demo ERP (`apps/api/tests/fixtures/`) is a fixture only - it is not
shipped and not needed to run the product.

## Adding an entity

1. Create or extend a reporting view (never point entities at raw transactional tables).
2. Add `entities/<area>.yaml` with a rich description, synonyms, dimensions, measures,
   time fields, `security.required_filters` and `forbidden_fields`.
3. Add joins, metrics, glossary terms and at least two examples.
4. Validate: `python scripts/dev.py semantic-validate` (and review the binding report of
   `GET /connections/{id}/semantic/status` after ingestion - unbound entities are ignored).
5. Re-run ingestion for the affected connections (Phase 3 ingestion API).
