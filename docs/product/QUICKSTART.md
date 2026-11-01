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
PADOS_DB_NAME=pados_dogfood_operational alembic upgrade 20261101_0020
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

No chat model is configured by default (`PADOS_CHAT_PROVIDER` unset). Cognitive actions then report
`COGNITIVE_PROVIDER_NOT_CONFIGURED` instead of silently using a stand-in. `PADOS_CHAT_PROVIDER=mock` is for contract
tests only. Its output is labeled `MOCK_CONTRACT_TEST_ONLY`, and `pados doctor` warns while it is selected. A local
Ollama model runs only under a measured execution binding. The model call runs after the routing transaction commits,
so no database lock is held while the model thinks.

**Verify the whole local path end to end (synthetic data, loopback, no provider credentials).**

```bash
# Linux/macOS: any PostgreSQL 16+ with pgvector you control; the password is read from PADOS_DB_PASSWORD
PADOS_DB_PASSWORD=... python scripts/dogfood/local_dogfood_e2e.py --pg-port 55441 --out runtime/artifacts/dogfood-e2e
# Windows: a private, trust-auth loopback cluster from the native PostgreSQL binaries (no password exists)
.venv\Scripts\python scripts\dogfood\local_dogfood_e2e.py --pg-bin "C:\Program Files\PostgreSQL\18\bin" --out runtime\artifacts\dogfood-e2e
```

It runs `pados init`, the lane migrations above, `pados doctor`, and `pados serve`. Through the control API it then:
- authenticates, and checks that a missing token and the public default token are refused;
- creates two synthetic projects (their proof ScopeVersion is initialized);
- takes a work request with a hashed TaskEnvelope through human approval to a goal, a plan and a deterministic
  TaskLoop run of `markdown_reference_check`, a read-only registered probe that checks that every relative Markdown
  reference resolves inside the project and reports a declared `pados.capability-outcome.v1` outcome; the loop
  reaches `COMPLETED` and the receipts carry SHA-256 digests;
- submits the completed loop as a ResultCandidate: verification passes and an approval is pending, nothing commits;
- in the second project a broken reference makes the outcome UNRESOLVED: the task waits for a human and its candidate
  is refused; a step outside the approval never runs; a finished loop cannot be paused or cancelled;
- restarts with `PADOS_REMOTE_CONFIG` (loopback, one approving actor, WebAuthn rp_id `localhost`), reads the same state
  and the pending candidate back, and enrolls a device through `/remote/v1/presence/*`;
- approves over real HTTP: a standing session is refused, a fresh WebAuthn step-up proof approves, the existing single
  CAS commits ScopeVersion 0 → 1, the consumed proof cannot be replayed, and after another restart the lineage graph
  shows the accepted result.

The WebAuthn device in that run is the software authenticator from `tests/remote/presence_support.py` (a real P-256
key; the production verifier checks it). It stands in for a phone or browser; it is not a hardware-key validation.
The run finishes with the existing ResultCandidate → step-up → single-CAS integration suite on dedicated lanes
(rejection, stale ScopeVersion, candidate substitution). No model provider is involved. Evidence is written to
`<out>/E2E_RESULT.json`.

After a TaskLoop reaches `COMPLETED`, submit its verified result for review:

```bash
pados loop submit-result-candidate --loop-id <completed-loop-uuid> --json
```

The command prints only the candidate digest, verification result, and pending approval reference. A successful
submission does not accept the result; a human must inspect and approve it through the existing WebAuthn step-up and
CAS flow. The loop must be `COMPLETED`, its task must carry a valid TaskEnvelope and matching product-goal bindings, and
the capability output must satisfy its declared semantic contract. A successful process exit or a plain text command
output is not sufficient. If any verification check fails or no current proof ScopeVersion can be read, no approval is
created. The same submission is available as `POST /control/loops/{loop_id}/result-candidate`.

To decide a pending candidate, configure Remote Operations (`docs/remote/REMOTE_OPERATIONS_V1.md`): `pados serve`
mounts `/remote/v1` on loopback when `PADOS_REMOTE_CONFIG` is set. Enroll your device from the PWA, add the descriptor
to your actor row, restart, then approve or reject from the PWA's approval view; each decision needs a fresh WebAuthn
step-up. Approval commits through the existing single CAS; rejection keeps the evidence and commits nothing.

**Optional: a local compute node.**

```bash
pados node serve --config examples/config/node-deterministic.json
pados node probe
```

The first command prints the node id; put it into `examples/config/compute-nodes-loopback.json`, set `authorized: true` and `project_ids`, and export `PADOS_COMPUTE_NODES_CONFIG` before running the probe. See `examples/config/README.md`.

**Important.** Never run `alembic upgrade heads` on the proof database; use the explicit revisions above.
