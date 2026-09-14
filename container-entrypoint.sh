#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p /app/logs/profiles /app/config
touch /app/logs/cron.log /app/logs/ops-daily.log
chown -R docker:docker /app/logs /app/config

# Cron has a deliberately small environment. Persist only the non-secret
# scheduler knobs that scheduled jobs need, using shell-safe quoting.
RUNTIME_ENV=/tmp/hh-runtime.env
bash "$SCRIPT_DIR/scripts/write-runtime-env.sh" "$RUNTIME_ENV"
chown docker:docker "$RUNTIME_ENV"
chmod 0600 "$RUNTIME_ENV"

# Cron runs the deterministic workers; the enhanced admin UI remains the
# foreground process so container health and lifecycle are easy to observe.
cron

exec su -s /bin/bash docker -c "cd /app && exec python -m uvicorn admin.ui_app:app --host 0.0.0.0 --port 8000"
