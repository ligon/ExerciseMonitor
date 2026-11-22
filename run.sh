#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
PROJECT_CACHE_DIR="$HOME/.cache/projects/$(basename "$PWD")-$(pwd -P | sha1sum | cut -c1-8)"
export PYTHONPYCACHEPREFIX="$PROJECT_CACHE_DIR/pycache"

if [ ! -x "$PROJECT_CACHE_DIR/venv/bin/uvicorn" ]; then
    echo "Setting up virtual environment in $PROJECT_CACHE_DIR/venv ..."
    python3 -m venv "$PROJECT_CACHE_DIR/venv"
    "$PROJECT_CACHE_DIR/venv/bin/pip" install --cache-dir "$PROJECT_CACHE_DIR/pip" \
        fastapi "uvicorn[standard]" python-multipart garminconnect garmin-fit-sdk pytest httpx
fi

PORT="${PORT:-8000}"
echo "Starting Stelo + Garmin 24/7 Monitor on http://0.0.0.0:${PORT} ..."
exec "$PROJECT_CACHE_DIR/venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port "$PORT"
