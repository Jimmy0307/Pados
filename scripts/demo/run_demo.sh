#!/usr/bin/env bash
# One-command PADOS demo on a disposable PostgreSQL (pgvector) container.
#   bash scripts/demo/run_demo.sh [--local-ai] [--keep]
# Requires: docker, a PADOS virtualenv on PATH (pados, alembic, python).
# The database password is random per run; nothing leaves this machine.
set -euo pipefail
LOCAL_AI=""; KEEP=0
for arg in "$@"; do case "$arg" in --local-ai) LOCAL_AI="--local-ai";; --keep) KEEP=1;; esac; done
C=pados-demo-pg; PORT=${PADOS_DEMO_DB_PORT:-55433}
ROOT=$(mktemp -d "${TMPDIR:-/tmp}/pados-demo-XXXXXX")
export PADOS_DB_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
docker run -d --rm --name $C --label pados.purpose=demo -e POSTGRES_DB=pados -e POSTGRES_USER=pados \
  -e POSTGRES_PASSWORD="$PADOS_DB_PASSWORD" -p 127.0.0.1:$PORT:5432 --tmpfs /var/lib/postgresql:rw,size=2g \
  pgvector/pgvector:pg18 >/dev/null
cleanup() { [ "$KEEP" = 1 ] || docker stop $C >/dev/null 2>&1 || true; }
trap cleanup EXIT
for _ in $(seq 1 60); do docker exec $C psql -U pados -d pados -tAc "select 1" >/dev/null 2>&1 && break; sleep 1; done
for db in pados_demo_operational pados_demo_proof pados_demo_corpus; do docker exec $C psql -U pados -d postgres -qc "CREATE DATABASE $db"; done
export PADOS_DB_HOST=127.0.0.1 PADOS_DB_PORT=$PORT PADOS_DB_USER=pados PADOS_API_HOST=127.0.0.1
export PADOS_DB_NAME=pados_demo_operational PADOS_OPERATIONAL_DATABASE_NAME=pados_demo_operational
export PADOS_PRODUCT_DATABASE_NAME=pados_demo_proof PADOS_CORPUS_DATABASE_NAME=pados_demo_corpus
export PADOS_PRODUCT_WORKSPACE_ROOT="$ROOT/workspace" PADOS_PRODUCT_OBJECT_ROOT="$ROOT/objects"
# Documented lane migrations (explicit revisions; never "upgrade heads").
PADOS_DB_NAME=pados_demo_operational alembic upgrade 20261010_0019 >/dev/null
PADOS_DB_NAME=pados_demo_proof alembic upgrade 20260730_0011 >/dev/null
PADOS_DB_NAME=pados_demo_proof alembic upgrade 20260808_0013 >/dev/null
PADOS_DB_NAME=pados_demo_proof alembic -c alembic_p6_19b.ini upgrade head >/dev/null
PADOS_DB_NAME=pados_demo_corpus alembic upgrade 20260730_0011 >/dev/null
pados doctor >/dev/null 2>&1 && echo "doctor: ok" || echo "doctor: see 'pados doctor --json'"
OUT=${PADOS_DEMO_OUT:-$ROOT/DEMO_UAT.json}
python examples/demo/public_demo.py $LOCAL_AI --root "$ROOT" --out "$OUT"
echo "demo evidence: $OUT"
