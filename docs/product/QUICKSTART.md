# Quick start (alpha, single machine)

```bash
git clone <repository> pados && cd pados
python3.12 -m venv .venv && . .venv/bin/activate
pip install -c requirements.lock -e ".[dev]"

# Fastest path: the one-command demo (disposable PostgreSQL in Docker, synthetic data, no external APIs)
bash scripts/demo/run_demo.sh            # deterministic local node
bash scripts/demo/run_demo.sh --local-ai # optional: a local Ollama; the model is chosen by discovery

# Your own local instance
pados init                               # writes .env with a unique control token (runtime/local/control.token)
docker compose up -d postgres            # or any PostgreSQL 16+ with pgvector; set PADOS_DB_PORT/PASSWORD in .env
# create databases pados_dogfood_operational, pados_dogfood_proof, pados_dogfood_corpus, then:
# `pados init` keeps the separate legacy Product Loop routes disabled. The proof lane below is for the governed
# P6.10 ResultCandidate acceptance flow only; enabling `/product-loop/*` is outside this dogfood scope.
PADOS_DB_NAME=pados_dogfood_operational alembic upgrade 20261010_0019
PADOS_DB_NAME=pados_dogfood_proof alembic upgrade 20260730_0011
PADOS_DB_NAME=pados_dogfood_proof alembic upgrade 20260808_0013
PADOS_DB_NAME=pados_dogfood_proof alembic -c alembic_p6_19b.ini upgrade head
PADOS_DB_NAME=pados_dogfood_corpus alembic upgrade 20260730_0011
pados doctor                             # Core readiness plus optional extension and security-posture sections
pados serve                              # API on 127.0.0.1:8088
cd web && npm ci && npm run dev          # UI on 127.0.0.1:5173 (Control Center -> Mission Control)
pados project create --name demo --root-path ./runtime/local/workspace
```

When a distinct loopback proof database is configured, `pados project create` initializes that project's first
ScopeVersion through the existing StateProof service. If project creation succeeds but proof-database initialization
is interrupted, retry it with `pados project initialize-proof-scope --project-id <project-uuid> --json`. The command
refuses remote or shared operational databases. This creates the initial scope only; it does not accept work or commit
semantic state.

**Verify the whole local path end to end (synthetic data, loopback, no provider credentials).**

```bash
# Linux/macOS: any PostgreSQL 16+ with pgvector you control; the password is read from PADOS_DB_PASSWORD
PADOS_DB_PASSWORD=... python scripts/dogfood/local_dogfood_e2e.py --pg-port 55441 --out runtime/artifacts/dogfood-e2e
# Windows: a private, trust-auth loopback cluster from the native PostgreSQL binaries (no password exists)
.venv\Scripts\python scripts\dogfood\local_dogfood_e2e.py --pg-bin "C:\Program Files\PostgreSQL\18\bin" --out runtime\artifacts\dogfood-e2e
```

It runs `pados init`, the lane migrations above, `pados doctor`, and `pados serve`. Through the control API it then:
- authenticates, and checks that a missing token and the public default token are refused;
- creates a project;
- takes a work request through human approval to a goal, a plan and a deterministic TaskLoop run; the Python-version
  probe's plain text output does not satisfy the declared semantic outcome contract, so the task waits for human review
  and cannot enter ResultCandidate approval;
- checks that a step outside the approval is refused and that unresolved work is not reported as completed;
- restarts the server and reads the same state back.

It finishes with the existing deterministic ResultCandidate → step-up approval → single-CAS integration suite on
dedicated lanes. That suite validates the governed acceptance path using synthetic fixture evidence; it is not live
provider validation. Evidence is written to `<out>/E2E_RESULT.json`.

After a TaskLoop reaches `COMPLETED`, submit its verified result for review:

```bash
pados loop submit-result-candidate --loop-id <completed-loop-uuid> --json
```

The command prints only the candidate digest, verification result, and pending approval reference. A successful
submission does not accept the result; a human must inspect and approve it through the existing WebAuthn step-up and
CAS flow. The loop must be `COMPLETED`, its task must carry a valid TaskEnvelope and matching product-goal bindings, and
the capability output must satisfy its declared semantic contract. A successful process exit or a plain text command
output is not sufficient. If any verification check fails or no current proof ScopeVersion can be read, no approval is
created.

**Optional: a local compute node.**

```bash
pados node serve --config examples/config/node-deterministic.json
pados node probe
```

The first command prints the node id; put it into `examples/config/compute-nodes-loopback.json`, set `authorized: true` and `project_ids`, and export `PADOS_COMPUTE_NODES_CONFIG` before running the probe. See `examples/config/README.md`.

**Important.** Never run `alembic upgrade heads` on the proof database; use the explicit revisions above.
