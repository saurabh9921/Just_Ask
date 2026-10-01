# Shared contracts

JSON Schema (draft 2020-12) generated from the API's Pydantic models — the models are
the source of truth. Regenerate after changing `app/query/plans.py`,
`app/schemas/{analytics,history,feedback,exports}.py` or `app/agents/state.py`:

```bash
apps/api/.venv/Scripts/python scripts/export_contracts.py        # write
apps/api/.venv/Scripts/python scripts/export_contracts.py --check  # CI / drift check
```

`apps/api/tests/unit/test_contracts.py` fails when these files are stale.

| File | Model |
|---|---|
| `query-plan.schema.json` | `QueryPlan` — the only thing the LLM may produce for data questions |
| `analytics-query-request.schema.json` | `POST /analytics/query`, `/explain` body |
| `analytics-follow-up-request.schema.json` | `POST /analytics/follow-up` body |
| `analytics-stream-request.schema.json` | `POST /analytics/query/stream` body |
| `analytics-query-response.schema.json` | `QueryResponse` |
| `analytics-query-record.schema.json` | `GET /analytics/query/{id}` |
| `analytics-cancel-response.schema.json` | `POST /analytics/query/{id}/cancel` |
| `analytics-progress-event.schema.json` | SSE `progress` event payload |
| `history-page.schema.json` | `GET /history` |
| `history-detail.schema.json` | `GET /history/{id}` |
| `history-update-request.schema.json` | `PATCH /history/{id}` and `PATCH /history/conversations/{id}` body |
| `history-conversation-page.schema.json` | `GET /history/conversations` (one entry per chat) |
| `history-conversation-detail.schema.json` | `GET /history/conversations/{id}` (the whole chat, oldest first) |
| `feedback-create-request.schema.json` | `POST /feedback` body |
| `feedback.schema.json` | `POST /feedback` response |
| `feedback-page.schema.json` | `GET /feedback` |
| `export-create-request.schema.json` | `POST /exports` body (Phase 6) |
| `export.schema.json` | `ExportOut` — `POST /exports`, `GET /exports/{id}` |
| `export-page.schema.json` | `GET /exports` |
| `export-download-token.schema.json` | `POST /exports/{id}/download-token` |
