#!/usr/bin/env python3
# ruff: noqa: T201, S603
"""Cross-platform developer task runner (the Makefile delegates here).

    python scripts/dev.py <task> [<task> ...]
    python scripts/dev.py help

Works on Windows, macOS and Linux without ``make``. Uses only the standard library.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "apps" / "api"
WORKER = ROOT / "apps" / "worker"
EXT = ROOT / "apps" / "browser-extension"
COMPOSE_FILE = ROOT / "infrastructure" / "docker" / "docker-compose.yml"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
PROD_ENV_FILE = ROOT / ".env.production"
PROD_ENV_EXAMPLE = ROOT / ".env.production.example"
PROD_COMPOSE_FILE = ROOT / "infrastructure" / "docker" / "docker-compose.prod.yml"

IS_WINDOWS = os.name == "nt"
VENV = API / ".venv"
VENV_BIN = VENV / ("Scripts" if IS_WINDOWS else "bin")
PY = VENV_BIN / ("python.exe" if IS_WINDOWS else "python")
NPM = "npm.cmd" if IS_WINDOWS else "npm"

PY_TARGETS = ["app", "tests", "../worker/erp_worker", "../worker/tests"]

TASKS: dict[str, tuple[Callable[[], None], str]] = {}


def task(description: str) -> Callable[[Callable[[], None]], Callable[[], None]]:
    def register(fn: Callable[[], None]) -> Callable[[], None]:
        TASKS[fn.__name__.replace("_", "-")] = (fn, description)
        return fn

    return register


def run(cmd: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print(
        f"\n$ {' '.join(cmd)}    (in {cwd.relative_to(ROOT) if cwd != ROOT else '.'})", flush=True
    )
    merged = {**os.environ, **(env or {})}
    result = subprocess.run(cmd, cwd=cwd, env=merged, check=False)
    if result.returncode != 0:
        sys.exit(result.returncode)


def need_venv() -> None:
    if not PY.exists():
        sys.exit("API virtualenv missing. Run: python scripts/dev.py install")


MIN_NODE = ((22, 22, 3), (24, 15, 0))  # Angular 22: ^22.22.3 || >=24.15


def node_ok() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    out = subprocess.run([node, "--version"], capture_output=True, text=True, check=False)
    parts = tuple(int(x) for x in re.findall(r"\d+", out.stdout)[:3])
    return (parts[0] == 22 and parts >= MIN_NODE[0]) or parts >= MIN_NODE[1]


def ext_ready() -> bool:
    """True when extension tasks can run; exits loudly if Node is too old."""
    if not ((EXT / "package.json").exists() and (EXT / "node_modules").exists()):
        return False
    if not node_ok():
        sys.exit(
            "Node.js ^22.22.3 or >=24.15 is required by Angular 22 for the browser extension "
            "(see apps/browser-extension/.nvmrc). Upgrade Node and re-run."
        )
    return True


def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def compose(*args: str) -> None:
    if not shutil.which("docker"):
        sys.exit("docker is not installed or not on PATH")
    run(["docker", "compose", "-f", str(COMPOSE_FILE), "--env-file", str(ENV_FILE), *args])


def compose_running_services() -> set[str]:
    """Services of the dev compose project that are running (empty if Docker is down)."""
    if not shutil.which("docker") or not ENV_FILE.exists():
        return set()
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "--env-file",
            str(ENV_FILE),
            "ps",
            "--status",
            "running",
            "--services",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return set(result.stdout.split()) if result.returncode == 0 else set()


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------- setup
@task("Create .env with freshly generated secrets (never overwrites an existing .env)")
def bootstrap() -> None:
    if ENV_FILE.exists():
        print(".env already exists - leaving it untouched")
    else:
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        generated: dict[str, str] = {}
        for key in (
            "POSTGRES_APP_PASSWORD",
            "REDIS_PASSWORD",
            "GRAFANA_ADMIN_PASSWORD",
            "JWT_SECRET",
            "ENCRYPTION_KEY",
        ):
            generated[key] = secrets.token_urlsafe(
                48 if key in {"JWT_SECRET", "ENCRYPTION_KEY"} else 24
            )
            text = re.sub(rf"^{key}=.*$", f"{key}={generated[key]}", text, flags=re.MULTILINE)
        pg = quote(generated["POSTGRES_APP_PASSWORD"], safe="")
        rd = quote(generated["REDIS_PASSWORD"], safe="")
        # A PostgreSQL already installed on this machine usually owns 5432.
        pg_port = 5433 if port_open("127.0.0.1", 5432) else 5432
        if pg_port != 5432:
            print("port 5432 is in use (local PostgreSQL?) - the platform database uses 5433")
        text = re.sub(
            r"^POSTGRES_HOST_PORT=.*$", f"POSTGRES_HOST_PORT={pg_port}", text, flags=re.MULTILINE
        )
        text = re.sub(
            r"^APP_DATABASE_URL=.*$",
            f"APP_DATABASE_URL=postgresql+asyncpg://app:{pg}@127.0.0.1:{pg_port}/erp_insight",
            text,
            flags=re.MULTILINE,
        )
        text = re.sub(
            r"^REDIS_URL=.*$", f"REDIS_URL=redis://:{rd}@127.0.0.1:6379/0", text, flags=re.MULTILINE
        )
        ENV_FILE.write_text(text, encoding="utf-8")
        print(".env created with random secrets (values not printed)")
    install()


@task("Create the Python virtualenv and install API/worker/extension dependencies")
def install() -> None:
    if not PY.exists():
        run([sys.executable, "-m", "venv", str(VENV)])
    run([str(PY), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(PY), "-m", "pip", "install", "-e", f"{API}[dev]"])
    run([str(PY), "-m", "pip", "install", "--no-deps", "-e", str(WORKER)])
    if (EXT / "package.json").exists():
        if not node_ok():
            sys.exit(
                "Node.js ^22.22.3 or >=24.15 is required for the extension; upgrade Node first."
            )
        run([NPM, "ci" if (EXT / "package-lock.json").exists() else "install"], cwd=EXT)


# ------------------------------------------------------------------- quality
@task("Auto-format Python (ruff) and TypeScript (prettier)")
def format() -> None:  # noqa: A001 - task name
    need_venv()
    run([str(VENV_BIN / "ruff"), "format", *PY_TARGETS], cwd=API)
    run([str(VENV_BIN / "ruff"), "check", "--fix", "--select", "I", *PY_TARGETS], cwd=API)
    if ext_ready():
        run([NPM, "run", "format", "--if-present"], cwd=EXT)


@task("Lint Python (ruff) and TypeScript (eslint); fails on any finding")
def lint() -> None:
    need_venv()
    run([str(VENV_BIN / "ruff"), "format", "--check", *PY_TARGETS], cwd=API)
    run([str(VENV_BIN / "ruff"), "check", *PY_TARGETS], cwd=API)
    if ext_ready():
        run([NPM, "run", "lint"], cwd=EXT)


@task("Type-check Python (mypy --strict) and TypeScript (tsc strict)")
def typecheck() -> None:
    need_venv()
    run([str(VENV_BIN / "mypy"), "app", "tests"], cwd=API)
    run([str(VENV_BIN / "mypy"), "erp_worker", "tests"], cwd=WORKER)
    if ext_ready():
        run([NPM, "run", "typecheck"], cwd=EXT)


# --------------------------------------------------------------------- tests
@task("Unit tests: API, worker and extension (no Docker needed)")
def test() -> None:
    need_venv()
    run([str(PY), "-m", "pytest", "tests/unit", "tests/contract", "tests/evals", "-q"], cwd=API)
    run([str(PY), "-m", "pytest", "-q"], cwd=WORKER)
    if ext_ready():
        run([NPM, "test"], cwd=EXT)


@task("Security regression tests")
def test_security() -> None:
    need_venv()
    run([str(PY), "-m", "pytest", "tests/security", "-q", "-m", "security"], cwd=API)


@task("Performance budgets (docs/performance.md)")
def test_performance() -> None:
    need_venv()
    run([str(PY), "-m", "pytest", "tests/performance", "-q", "-m", "performance"], cwd=API)


@task("Dependency vulnerability audit (pip-audit + npm audit)")
def audit() -> None:
    need_venv()
    run([str(VENV_BIN / "pip-audit"), "--progress-spinner", "off", "--skip-editable"], cwd=API)
    if ext_ready():
        run([NPM, "audit", "--audit-level=high"], cwd=EXT)


@task("Evaluation harness: labelled questions, safety gate (sandbox, configured LLM)")
def eval() -> None:  # noqa: A001 - task name
    need_venv()
    reports = ROOT / "evals" / "reports"
    reports.mkdir(exist_ok=True)
    run(
        [
            str(PY),
            str(ROOT / "scripts" / "run_evals.py"),
            "--markdown",
            str(reports / "latest.md"),
            "--json",
            str(reports / "latest.json"),
        ],
        cwd=ROOT,
    )


@task("Integration tests against docker compose services (postgres, redis, qdrant)")
def test_integration() -> None:
    need_venv()
    env = read_env_file()
    extra: dict[str, str] = {}
    pg_port = int(env.get("POSTGRES_HOST_PORT") or 5432)
    # Only the platform's own compose Postgres - never another server that happens to
    # listen on the port (e.g. a locally installed PostgreSQL).
    if (
        "postgres" in compose_running_services()
        and port_open("127.0.0.1", pg_port)
        and env.get("POSTGRES_APP_PASSWORD")
    ):
        pw = quote(env["POSTGRES_APP_PASSWORD"], safe="")
        user = env.get("POSTGRES_APP_USER", "app")
        extra["TEST_POSTGRES_ADMIN_URL"] = (
            f"postgresql+asyncpg://{user}:{pw}@127.0.0.1:{pg_port}/postgres"
        )
    if port_open("127.0.0.1", 6379) and env.get("REDIS_PASSWORD"):
        extra["TEST_REDIS_URL"] = (
            f"redis://:{quote(env['REDIS_PASSWORD'], safe='')}@127.0.0.1:6379/15"
        )
    if port_open("127.0.0.1", 6333):
        extra["TEST_QDRANT_URL"] = "http://127.0.0.1:6333"
    missing = {"postgres", "redis", "qdrant"} - {k.split("_")[1].lower() for k in extra}
    if missing:
        print(
            f"WARNING: services not reachable: {sorted(missing)} - their tests will be SKIPPED.\n"
            "Start them with: python scripts/dev.py up-data",
            flush=True,
        )
    # tests/contract runs the PostgreSQL leg of the adapter contract when PG is up.
    run(
        [str(PY), "-m", "pytest", "tests/integration", "tests/contract", "-q", "-rs"],
        cwd=API,
        env=extra,
    )


@task("format-check + lint + typecheck + unit + security + performance + integration")
def check() -> None:
    lint()
    typecheck()
    test()
    test_security()
    test_performance()
    test_integration()


# -------------------------------------------------------------------- docker
@task("Start the full stack (docker compose up -d --build)")
def up() -> None:
    compose("up", "-d", "--build")


# ---------------------------------------------------------------- production
PROD_SECRETS = (
    "POSTGRES_APP_PASSWORD",
    "REDIS_PASSWORD",
    "GRAFANA_ADMIN_PASSWORD",
    "QDRANT_API_KEY",
    "JWT_SECRET",
    "ENCRYPTION_KEY",
)


@task("Create .env.production (random secrets) from .env.production.example")
def bootstrap_prod() -> None:
    if PROD_ENV_FILE.exists():
        print(".env.production already exists - leaving it untouched")
        return
    text = PROD_ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in PROD_SECRETS:
        value = secrets.token_urlsafe(48 if key in {"JWT_SECRET", "ENCRYPTION_KEY"} else 32)
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.MULTILINE)
    PROD_ENV_FILE.write_text(text, encoding="utf-8", newline="\n")
    print(".env.production created with random secrets (values not printed).")
    print("Now edit every REPLACE_* value in it, put TLS certificates in TLS_CERT_DIR,")
    print("then run: python scripts/dev.py prod-up")


def prod_compose(*args: str) -> None:
    if not shutil.which("docker"):
        sys.exit("docker is not installed or not on PATH")
    if not PROD_ENV_FILE.exists():
        sys.exit(".env.production is missing - run: python scripts/dev.py bootstrap-prod")
    env = read_env_file(PROD_ENV_FILE)
    profiles = (
        ["--profile", "ollama"]
        if env.get("LLM_PROVIDER") == "ollama" and "ollama" in (env.get("LLM_BASE_URL") or "")
        else []
    )
    run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "-f",
            str(PROD_COMPOSE_FILE),
            "--env-file",
            str(PROD_ENV_FILE),
            *profiles,
            *args,
        ]
    )


def check_prod_env() -> None:
    env = read_env_file(PROD_ENV_FILE)
    problems = [k for k, v in env.items() if "REPLACE" in v]
    problems += [k for k in PROD_SECRETS if env.get(k, "") in {"", "replace-in-development"}]
    cert_dir = Path(env.get("TLS_CERT_DIR") or "/nonexistent")
    if not (cert_dir / "fullchain.pem").exists() or not (cert_dir / "privkey.pem").exists():
        problems.append(f"TLS_CERT_DIR ({cert_dir}) must contain fullchain.pem and privkey.pem")
    if problems:
        sys.exit(".env.production is not ready: " + ", ".join(problems))


@task("Production: validate .env.production, build and start (HTTPS on 443)")
def prod_up() -> None:
    check_prod_env()
    prod_compose("up", "-d", "--build")
    print("\nStarted. Next: dev.py prod-create-tenant <slug> <name>, prod-create-user, onboard")


@task("Production: stop the stack (volumes are kept)")
def prod_down() -> None:
    prod_compose("down")


@task("Production: follow logs")
def prod_logs() -> None:
    prod_compose("logs", "-f", "--tail", "200")


@task(
    "Production: create a tenant inside the API container: dev.py prod-create-tenant <slug> <name>"
)
def prod_create_tenant() -> None:
    slug, name = _args(2, "prod-create-tenant <slug> <name>")
    prod_compose(
        "exec", "api", "python", "-m", "app.cli", "create-tenant", "--slug", slug, "--name", name
    )


@task(
    "Production: create a user (password prompted): dev.py prod-create-user <tenant> <email> <role>"
)
def prod_create_user() -> None:
    tenant, email, role = _args(3, "prod-create-user <tenant> <email> <role>")
    prod_compose(
        "exec",
        "api",
        "python",
        "-m",
        "app.cli",
        "create-user",
        "--tenant",
        tenant,
        "--email",
        email,
        "--display-name",
        email.split("@")[0],
        "--role",
        role,
    )


@task("Start only postgres, redis and qdrant (for host-side dev and integration tests)")
def up_data() -> None:
    compose("up", "-d", "--wait", "postgres", "redis", "qdrant")


@task("Stop the stack (keeps volumes)")
def down() -> None:
    compose("down")


@task("Follow logs of all services")
def logs() -> None:
    compose("logs", "-f", "--tail", "200")


# ---------------------------------------------------------------- migrations
@task("Apply database migrations (alembic upgrade head) using APP_DATABASE_URL")
def migrate() -> None:
    need_venv()
    run([str(VENV_BIN / "alembic"), "upgrade", "head"], cwd=API, env=read_env_file())


@task("Print migration SQL for DBA review without touching a database")
def migrate_sql() -> None:
    need_venv()
    env = {"APP_DATABASE_URL": "postgresql+asyncpg://review@localhost/review"}
    run([str(VENV_BIN / "alembic"), "upgrade", "head", "--sql"], cwd=API, env=env)


# ----------------------------------------------------------------- extension
@task("Build the browser extension (API origin from EXTENSION_API_ORIGIN in .env)")
def ext_build() -> None:
    if not ext_ready():
        sys.exit("Extension dependencies missing. Run: python scripts/dev.py install")
    origin = read_env_file().get("EXTENSION_API_ORIGIN", "")
    run([NPM, "run", "build"], cwd=EXT, env={"ERP_API_ORIGIN": origin} if origin else None)


@task("Run the API locally with auto-reload (needs: up-data, migrate)")
def api() -> None:
    need_venv()
    env = read_env_file() | {"LOG_JSON": "false"}
    run([str(VENV_BIN / "uvicorn"), "app.main:app", "--reload", "--port", "8000"], cwd=API, env=env)


@task("Validate the semantic-layer YAML (no database needed)")
def semantic_validate() -> None:
    need_venv()
    run([str(PY), str(ROOT / "scripts" / "ingest_semantic_layer.py"), "--validate-only"])


@task("Run the mock OpenAI/Ollama-compatible model server on :8001 (no GPU)")
def mock_llm() -> None:
    need_venv()
    run([str(VENV_BIN / "uvicorn"), "app.llm.mock_server:app", "--port", "8001"], cwd=API)


@task("Onboard a live database: dev.py onboard <connect|draft|ingest|status> ... (see --help)")
def onboard() -> None:
    need_venv()
    env = read_env_file()
    env.setdefault("SEMANTIC_LAYER_PATH", str(ROOT / "semantic-layer"))
    run([str(PY), "-m", "app.onboarding", *(EXTRA_ARGS or ["--help"])], cwd=API, env=env)


@task("Create a tenant: dev.py create-tenant <slug> <name>")
def create_tenant() -> None:
    need_venv()
    slug, name = _args(2, "create-tenant <slug> <name>")
    run(
        [str(PY), "-m", "app.cli", "create-tenant", "--slug", slug, "--name", name],
        cwd=API,
        env=read_env_file(),
    )


@task("Create a user (password prompted): dev.py create-user <tenant> <email> <role>")
def create_user() -> None:
    need_venv()
    tenant, email, role = _args(3, "create-user <tenant> <email> <role>")
    run(
        [
            str(PY),
            "-m",
            "app.cli",
            "create-user",
            "--tenant",
            tenant,
            "--email",
            email,
            "--display-name",
            email.split("@")[0],
            "--role",
            role,
        ],
        cwd=API,
        env=read_env_file(),
    )


@task("Reset a password (prompted): dev.py set-password <tenant> <email>")
def set_password() -> None:
    need_venv()
    tenant, email = _args(2, "set-password <tenant> <email>")
    run(
        [str(PY), "-m", "app.cli", "set-password", "--tenant", tenant, "--email", email],
        cwd=API,
        env=read_env_file(),
    )


EXTRA_ARGS: list[str] = []


def _args(count: int, usage: str) -> list[str]:
    if len(EXTRA_ARGS) != count:
        sys.exit(f"usage: python scripts/dev.py {usage}")
    return EXTRA_ARGS


@task("List tasks")
def help() -> None:  # noqa: A001 - task name
    width = max(len(n) for n in TASKS)
    for name, (_, desc) in TASKS.items():
        print(f"  {name.ljust(width)}  {desc}")


def main(argv: list[str]) -> None:
    if argv and argv[0] in {
        "create-tenant",
        "create-user",
        "onboard",
        "prod-create-tenant",
        "prod-create-user",
        "set-password",
    }:
        EXTRA_ARGS.extend(argv[1:])
        argv = argv[:1]
    names = argv or ["help"]
    for name in names:
        if name not in TASKS:
            print(f"unknown task: {name}\n")
            help()
            sys.exit(2)
    for name in names:
        TASKS[name][0]()


if __name__ == "__main__":
    main(sys.argv[1:])
