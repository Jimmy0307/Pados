#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Online Linux validation of ONE pushed commit on a Linux VM (not WSL): a fresh clone from GitHub at the exact SHA, a
# fresh venv from requirements.lock (as CI), then the live dogfood E2E, the CI gates, the CI wheel build (with build
# isolation) and the canonical PostgreSQL integration gate. Each database runs in its own disposable container,
# removed with its volume.
#
# Usage: vm_online_chain.sh <sha> <branch> [run tag]
#   The run tag (e.g. -r2) names a rerun of the same SHA: an earlier run's clone and -out directory are evidence and
#   are never overwritten. Environment: PADOS_VALIDATION_BASE (default $HOME/pados-worktrees), PADOS_REPO_URL.
# Evidence: <base>/online-<short sha><tag>-out/summary.json (the vmware-linux-ci-gates lane record, format vm-summary)
# and e2e/E2E_RESULT.json (the vmware-linux-e2e lane record).
set -u
if [ $# -lt 2 ]; then echo "usage: $0 <sha> <branch> [run tag]" >&2; exit 2; fi
SHA="$1"; BRANCH="$2"; SHORT="${SHA:0:7}"; TAG="${3:-}"
BASE="${PADOS_VALIDATION_BASE:-$HOME/pados-worktrees}"
REPO_URL="${PADOS_REPO_URL:-https://github.com/Jimmy0307/Pados.git}"
ROOT="$BASE/online-$SHORT$TAG"; OUT="$BASE/online-$SHORT$TAG-out"
# Never overwrite an earlier run: its clone and -out directory are evidence.
if [ -e "$ROOT" ] || [ -e "$OUT" ]; then
  echo "REFUSE_EXISTING_RUN $ROOT (pass a new run tag as the third argument, e.g. -r2)" >&2; exit 9
fi
# A run needs about 1 GB that stays, plus build and test scratch.
mkdir -p "$BASE"
AVAIL_KB=$(df -Pk "$BASE" | awk 'NR==2{print $4}')
if [ "$AVAIL_KB" -lt 3145728 ]; then echo "REFUSE_LOW_DISK avail_kb=$AVAIL_KB (need 3 GiB)" >&2; exit 8; fi
mkdir -p "$OUT"; exec > >(tee "$OUT/run.log") 2>&1
git clone -q --single-branch --branch "$BRANCH" "$REPO_URL" "$ROOT" || exit 10
cd "$ROOT" && git checkout -q --detach "$SHA" || exit 11
[ "$(git rev-parse HEAD)" = "$SHA" ] || exit 12
echo "HEAD $(git rev-parse HEAD) host=$(hostname) virt=$(systemd-detect-virt) mode=online-clone"
python3.12 -m venv .venv && . .venv/bin/activate || exit 13
python -m pip install -q --upgrade pip && python -m pip install -q -c requirements.lock -e ".[dev]" || exit 14
python -c "import pados,sys; p=pados.__file__; print('pados from', p); sys.exit(0 if p.startswith('$ROOT/src') else 1)" || exit 15
export PYTHONUTF8=1

# 1. Live dogfood E2E (its own disposable PostgreSQL container).
PG_E2E="pados-online-e2e-$SHORT"; E2E_PORT=55441
E2E_PASS="$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
docker rm -fv "$PG_E2E" >/dev/null 2>&1
docker run -d --name "$PG_E2E" -e POSTGRES_DB=pados -e POSTGRES_USER=pados -e POSTGRES_PASSWORD="$E2E_PASS" \
  -p 127.0.0.1:$E2E_PORT:5432 pgvector/pgvector:pg16 >/dev/null || exit 16
for _ in $(seq 1 60); do docker exec "$PG_E2E" pg_isready -U pados >/dev/null 2>&1 && break; sleep 2; done; sleep 3
PADOS_DB_PASSWORD="$E2E_PASS" python scripts/dogfood/local_dogfood_e2e.py --pg-host 127.0.0.1 --pg-port $E2E_PORT \
  --pg-user pados --out "$OUT/e2e" > "$OUT/e2e.log" 2>&1
E2E_CODE=$?
docker rm -fv "$PG_E2E" >/dev/null 2>&1
echo "VM_DOGFOOD_E2E_EXIT=$E2E_CODE :: $(tail -n 1 "$OUT/e2e.log")"

# 2. CI gates (mirrors .github/workflows/ci.yml, linux job).
PG="pados-online-ci-$SHORT"
docker rm -fv "$PG" >/dev/null 2>&1
docker run -d --name "$PG" -e POSTGRES_DB=pados -e POSTGRES_USER=pados -e POSTGRES_PASSWORD=ci-only-disposable \
  -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16 >/dev/null || exit 17
for _ in $(seq 1 60); do docker exec "$PG" pg_isready -U pados >/dev/null 2>&1 && break; sleep 2; done; sleep 3
export PADOS_DB_PASSWORD=ci-only-disposable PADOS_DB_HOST=127.0.0.1 PADOS_DB_PORT=55432 PADOS_DB_USER=pados
# Provision and migrate the test databases exactly as .github/workflows/ci.yml (linux job) does.
export PADOS_DB_NAME=pados_p6_7_validation PADOS_OPERATIONAL_DATABASE_NAME=pados_local_runtime
export PADOS_CORPUS_DATABASE_NAME=pados_corpus_runtime
python - <<'PY' || echo "PROVISION_FAILED"
import os
import psycopg
from psycopg import sql
s = {"host": os.environ["PADOS_DB_HOST"], "port": int(os.environ["PADOS_DB_PORT"]), "user": "pados",
     "password": os.environ["PADOS_DB_PASSWORD"]}
names = ("pados_p6_7_validation", "pados_local_runtime", "pados_corpus_runtime")
with psycopg.connect(dbname="postgres", autocommit=True, **s) as admin:
    for name in names:
        if admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is None:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
for name in names:
    with psycopg.connect(dbname=name, autocommit=True, **s) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
print("provisioned")
PY
python -m alembic upgrade heads > "$OUT/migrate.log" 2>&1 || echo "MIGRATE_FAILED"
PADOS_DB_NAME=pados_local_runtime python -m alembic upgrade "$(python -c 'from pados.config import OPERATIONAL_REVISION_DEFAULT as r; print(r)')" >> "$OUT/migrate.log" 2>&1 || echo "MIGRATE_FAILED"
PADOS_DB_NAME=pados_corpus_runtime python -m alembic upgrade "$(python -c 'from pados.config import CORPUS_REVISION_DEFAULT as r; print(r)')" >> "$OUT/migrate.log" 2>&1 || echo "MIGRATE_FAILED"
declare -A RESULT
run() { local name="$1"; shift; echo "=== $name"; "$@" > "$OUT/$name.log" 2>&1; RESULT[$name]=$?; echo "$name exit=${RESULT[$name]} :: $(tail -n 2 "$OUT/$name.log" | tr '\n' ' ' | cut -c1-200)"; }
run ci_policy python scripts/release/ci_policy_check.py "$OUT/ci-policy.json"
run ruff ruff check src tests
run mypy mypy src
run mypy_win32 mypy --platform win32 src/pados/execution src/pados/product_loop src/pados/enterprise src/pados/compute_node src/pados/release
run compileall python -m compileall -q src tests
run migration_gate python scripts/release/migration_gate.py "$OUT/migration-gate.json"
run d1 python scripts/release/regression_gate.py D1 "$OUT/d1.json" --no-skips --min-tests 24 tests/unit/test_rc2_approval_subject.py tests/contract/test_rc2_route_surface_contract.py
run d2 python scripts/release/regression_gate.py D2 "$OUT/d2.json" --no-skips --min-tests 50 --allow-skip tests/local_intelligence/test_measured_execution_contract.py::test_the_accepted_m3_profile_still_loads_with_its_id_and_is_refused tests/local_intelligence/test_rc2_execution_binding.py tests/local_intelligence/test_measured_execution_contract.py tests/unit/test_rc2_graph_measured_binding.py
run d4 python scripts/release/regression_gate.py D4 "$OUT/d4.json" --no-skips --min-tests 17 tests/unit/test_rc2_worker_model_executor.py tests/unit/test_p6_10_loop_engine.py
run pack_focused python -m pytest -q -p no:cacheprovider tests/capability tests/packs/problem_solving tests/pipeline/test_pipeline_boundaries.py
run full_pytest python -m pytest -q -rs -p no:cacheprovider tests --ignore=tests/integration
run public_export python scripts/release/public_export_check.py "$OUT/public-export.json"
run secret_scan python scripts/release/secret_scan.py "$OUT/secret-scan.json"
run wheel_build sh -c "python -m pip wheel . --no-deps -w '$OUT/dist' && python -c \"import zipfile,glob;n=zipfile.ZipFile(glob.glob('$OUT/dist/*.whl')[0]).namelist();assert all(any(m in x for x in n) for m in ('pados/compute_node/provider.py','pados/lifecycle/storage_flexibility.py'))\""
run operability_gate python scripts/release/operability_gate.py "$OUT/operability-gate.json"
run doc_lint python scripts/release/doc_lint.py "$OUT/doc-lint.json"
run frontend sh -c "cd web && npm ci --ignore-scripts && npm run typecheck && npm test && npm run lint && npm run build"
run npm_cli_tests node --test packages/cli/test/inst1-init-consent.test.js packages/cli/test/cli-services.test.js packages/cli/test/config-runtime.test.js packages/cli/test/home-platform.test.js packages/cli/test/review-fixes.test.js
# 3. Canonical PostgreSQL integration gate (all suites, skips refused).
run integration_pg env -u PADOS_DB_NAME -u PADOS_OPERATIONAL_DATABASE_NAME -u PADOS_CORPUS_DATABASE_NAME \
  python scripts/release/postgres_integration_gate.py "$OUT/postgres-gate" --recreate
docker rm -fv "$PG" >/dev/null 2>&1
{
  echo "{\"sha\": \"$SHA\", \"host\": \"$(hostname)\", \"virt\": \"$(systemd-detect-virt)\", \"mode\": \"online-clone\", \"finished\": \"$(date -Is)\", \"e2e\": $E2E_CODE, \"gates\": {"
  first=1
  for name in ci_policy ruff mypy mypy_win32 compileall migration_gate d1 d2 d4 pack_focused full_pytest public_export secret_scan wheel_build operability_gate doc_lint frontend npm_cli_tests integration_pg; do
    [ $first -eq 1 ] || echo ","
    first=0
    printf '  "%s": %s' "$name" "${RESULT[$name]}"
  done
  echo; echo "}}"
} > "$OUT/summary.json"
cat "$OUT/summary.json"
echo "VM_ONLINE_CHAIN_DONE"
