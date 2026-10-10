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
# create databases pados_local_runtime, pados_proof_runtime, pados_corpus_runtime, then:
PADOS_DB_NAME=pados_local_runtime alembic upgrade 20261010_0018
PADOS_DB_NAME=pados_proof_runtime alembic upgrade 20260730_0011
PADOS_DB_NAME=pados_proof_runtime alembic upgrade 20260808_0013
PADOS_DB_NAME=pados_proof_runtime alembic -c alembic_p6_19b.ini upgrade head
PADOS_DB_NAME=pados_corpus_runtime alembic upgrade 20260730_0011
pados doctor                             # Core readiness plus optional extension and security-posture sections
pados serve                              # API on 127.0.0.1:8088
cd web && npm ci && npm run dev          # UI on 127.0.0.1:5173 (Control Center -> Mission Control)
pados project create --name demo --root-path ./runtime/local/workspace
```

**Optional: a local compute node.**

```bash
pados node serve --config examples/config/node-deterministic.json
pados node probe
```

The first command prints the node id; put it into `examples/config/compute-nodes-loopback.json`, set `authorized: true` and `project_ids`, and export `PADOS_COMPUTE_NODES_CONFIG` before running the probe. See `examples/config/README.md`.

**Important.** Never run `alembic upgrade heads` on the proof database; use the explicit revisions above.
