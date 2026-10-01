# Your semantic layer

This directory describes **your** ERP data to ERP Insight AI: which tables or views may be
queried, what the business names of their columns are, how numbers aggregate, which
columns are sensitive, and how entities join. The assistant can only answer questions
about what is defined here. It ships **empty** except for the sensitive-field policy.

| Folder | Contents |
|---|---|
| `entities/` | one YAML file per business entity (source view, dimensions, measures, time fields, security) |
| `joins/` | the only joins the query compiler may generate |
| `metrics/` | derived metrics (ratios, differences) built from measures |
| `synonyms/` | business glossary and abbreviations |
| `examples/` | question → plan examples (few-shot context for the model) |
| `policies/sensitive-fields.yaml` | column-name patterns that are never exposed (keep it) |

## Build it from your live database

```bash
python scripts/dev.py onboard connect --name "ERP production" --type postgresql \
    --host erp-db.internal --database erp --schema reporting --db-user erp_insight_ro \
    --tenant <tenant> --email <admin email>
python scripts/dev.py onboard draft --name "ERP production" --objects "sales_*,customers" \
    --tenant <tenant> --email <admin email>
#   review and edit the generated entities/*.yaml and joins/*-draft-joins.yaml
python scripts/dev.py onboard ingest --name "ERP production" --tenant <tenant> --email <admin email>
```

`draft` never exposes columns that match the sensitive-field policy (they are listed under
`security.forbidden_fields`), never overwrites existing files (unless `--force`), and
validates the result before writing. Review every draft: give fields business names,
descriptions and synonyms, delete measures that make no sense to sum, and add
`security.required_filters` if the database holds several companies' data.

Edits take effect after `onboard ingest` (the API reloads the YAML automatically; the
index is rebuilt by ingestion). Validate at any time with
`python scripts/dev.py semantic-validate`. Full reference: `docs/semantic-layer.md`.
