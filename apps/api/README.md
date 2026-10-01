# erp-insight-api

FastAPI backend for ERP Insight AI. See the repository root `README.md`,
`CLAUDE.md` and `docs/architecture.md`.

```bash
python -m venv .venv && . .venv/Scripts/activate   # or .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload            # needs Postgres/Redis/Qdrant (docker compose)
pytest tests/unit tests/security         # no Docker required
```
