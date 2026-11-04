#!/bin/sh
# Container entrypoint: migrate, then serve API + embedded worker.
set -e
python -m db.migrate
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-7860}" \
  --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 20 --no-access-log
