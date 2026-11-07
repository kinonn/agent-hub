#!/bin/sh
set -eu

cd /app
umask 077
python -c "from src.database.checks import check_database_filesystem; check_database_filesystem()"
alembic upgrade head
python -m src.database.startup
exec uvicorn src.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips='*'
