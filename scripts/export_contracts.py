#!/usr/bin/env python3
"""Export shared JSON Schema contracts to ``packages/contracts/``.

The API's Pydantic models are the source of truth; the extension (Phase 5) and
evaluation tooling consume the generated schemas. ``--check`` exits non-zero when
the committed files are stale (``tests/unit/test_contracts.py`` runs the same check).

    apps/api/.venv/Scripts/python scripts/export_contracts.py [--check]
"""

# ruff: noqa: T201
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from app.contracts import render_contracts  # noqa: E402

OUT = ROOT / "packages" / "contracts"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if files are out of date")
    args = parser.parse_args(argv)
    stale = []
    for name, schema in render_contracts().items():
        path = OUT / name
        text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != text:
            stale.append(name)
            if not args.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(text.encode("utf-8"))
    if args.check and stale:
        print("stale contracts: " + ", ".join(stale) + " (run scripts/export_contracts.py)")
        return 1
    print("contracts up to date" if not stale else "updated: " + ", ".join(stale))
    return 0


if __name__ == "__main__":
    sys.exit(main())
