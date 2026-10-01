#!/usr/bin/env python3
"""Run the labelled evaluation questions (evals/questions.yaml) and score the answers.

Sandbox mode (default, no Docker, source checkout only): an in-process API on the
demo ERP *test fixture* (SQLite) with its labelled questions, the LLM configured by the
usual settings (LLM_PROVIDER=mock by default; set LLM_PROVIDER=ollama/vllm +
LLM_BASE_URL/LLM_MODEL to evaluate a real model's planning ability):

    apps/api/.venv/Scripts/python scripts/run_evals.py
    apps/api/.venv/Scripts/python scripts/run_evals.py --markdown report.md --json report.json

Remote mode evaluates a running deployment on YOUR live data with YOUR labelled
questions (see evals/questions.example.yaml; result correctness is not scored):

    ERP_EVAL_PASSWORD=... [ERP_EVAL_UNGRANTED_PASSWORD=...] \\
      apps/api/.venv/Scripts/python scripts/run_evals.py --mode remote \\
      --dataset evals/questions.yaml --api-url https://erp-insight.example.com \\
      --tenant acme --email analyst@acme.example --connection-id <uuid> \\
      [--ungranted-email viewer@acme.example]

Exit codes: 0 = safety gate passed (and --min-pass-rate met), 1 = gate failed,
2 = the run could not be set up. Passwords are read from the environment only.
"""

# ruff: noqa: T201
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "apps" / "api"
sys.path.insert(0, str(API))
os.environ.setdefault("APP_ENV", "development")

import httpx  # noqa: E402

from app.config import Settings  # noqa: E402
from app.evals.dataset import load_dataset  # noqa: E402
from app.evals.remote import RemoteLoginError, RemoteTarget  # noqa: E402
from app.evals.runner import EvalReport, render_markdown, run_evaluation  # noqa: E402
from app.semantic.loader import load_semantic_layer  # noqa: E402
from tests.evals.sandbox import SandboxError, open_sandbox  # noqa: E402

# The demo ERP fixture questions (sandbox mode only).
DEMO_DATASET = API / "tests" / "fixtures" / "eval-questions.yaml"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mode", choices=["sandbox", "remote"], default="sandbox")
    parser.add_argument("--dataset", type=Path, help="labelled questions (required in remote mode)")
    parser.add_argument("--json", type=Path, help="write the full report as JSON")
    parser.add_argument("--markdown", type=Path, help="write a Markdown report")
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=0.0,
        help="fail when the overall case pass rate is below this (0..1)",
    )
    parser.add_argument("--api-url")
    parser.add_argument("--tenant")
    parser.add_argument("--email")
    parser.add_argument("--ungranted-email")
    parser.add_argument("--connection-id")
    return parser.parse_args(argv)


async def run_sandbox(args: argparse.Namespace) -> EvalReport:
    dataset = load_dataset(args.dataset or DEMO_DATASET)
    base = Settings()
    async with open_sandbox(base) as box:
        return await run_evaluation(
            dataset,
            box,
            box.bundle,
            gold=box.gold,
            target_name="sandbox (in-process API, SQLite demo ERP)",
            provider=box.settings.llm_provider.value,
        )


async def run_remote(args: argparse.Namespace) -> EvalReport:
    required = ("dataset", "api_url", "tenant", "email", "connection_id")
    missing = [n for n in required if not getattr(args, n)]
    password = os.environ.get("ERP_EVAL_PASSWORD")
    if missing or not password:
        need = [f"--{m.replace('_', '-')}" for m in missing]
        need += [] if password else ["ERP_EVAL_PASSWORD (environment)"]
        raise SystemExit(f"remote mode needs: {', '.join(need)}")
    dataset = load_dataset(args.dataset)
    bundle = load_semantic_layer(Settings().semantic_layer_path)
    async with httpx.AsyncClient(base_url=args.api_url.rstrip("/"), timeout=180) as client:
        target = RemoteTarget(client, args.connection_id)
        await target.login("analyst", args.tenant, args.email, password)
        ungranted_password = os.environ.get("ERP_EVAL_UNGRANTED_PASSWORD")
        if args.ungranted_email and ungranted_password:
            await target.login("ungranted", args.tenant, args.ungranted_email, ungranted_password)
        return await run_evaluation(
            dataset, target, bundle, gold=None, target_name=args.api_url, provider="remote"
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        runner = run_sandbox if args.mode == "sandbox" else run_remote
        report = asyncio.run(runner(args))
    except (SandboxError, RemoteLoginError, httpx.HTTPError, ValueError) as exc:
        print(f"evaluation could not run: {exc}", file=sys.stderr)
        return 2
    markdown = render_markdown(report)
    if args.markdown:
        args.markdown.write_text(markdown, encoding="utf-8")
    if args.json:
        args.json.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    for metric in report.metrics:
        rate = "n/a" if metric.rate is None else f"{metric.rate:.1%}"
        flag = " (safety)" if metric.safety else ""
        print(f"{metric.name}{flag}: {metric.passed}/{metric.total} = {rate}")
    lat = report.latency_ms
    print(f"latency ms p50={lat['p50']} p95={lat['p95']} max={lat['max']}")
    print(f"tokens total={report.tokens['total']:.0f} mean={report.tokens['mean_per_question']}")
    overall = report.metric("overall_pass").rate or 0.0
    if not report.safety_ok:
        print("SAFETY GATE FAILED: every safety metric must be 100%", file=sys.stderr)
        return 1
    if overall < args.min_pass_rate:
        print(f"pass rate {overall:.1%} is below --min-pass-rate {args.min_pass_rate:.1%}")
        return 1
    print("safety gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
