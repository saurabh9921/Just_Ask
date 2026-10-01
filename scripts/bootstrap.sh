#!/usr/bin/env sh
# One-time local setup: generates .env with random secrets, creates the Python
# virtualenv, installs API/worker/extension dependencies.
# Windows users can run the same thing with: python scripts/dev.py bootstrap
set -eu
cd "$(dirname "$0")/.."
command -v python3 >/dev/null 2>&1 && PY=python3 || PY=python
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "Python 3.12+ is required")'
command -v node >/dev/null 2>&1 || { echo "Node.js 20.19+/22.12+ is required for the extension"; exit 1; }
"$PY" scripts/dev.py bootstrap
echo
echo "Next steps:"
echo "  python scripts/dev.py up            # full stack via docker compose"
echo "  python scripts/dev.py ext-build     # build the extension, then load apps/browser-extension/dist unpacked"
