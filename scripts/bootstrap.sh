#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
(cd frontend && npm install && npm run build)
.venv/bin/python -m playwright install chromium
mkdir -p data
.venv/bin/alembic upgrade head

