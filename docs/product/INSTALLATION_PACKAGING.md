# Installation and packaging (V1 matrix)

| Channel | V1 decision | Notes |
|---|---|---|
| Python package in a venv (`pip install -e .` or a wheel) | **Supported** | One package for the CLI, API, node daemon and SDKs. `pip wheel . --no-deps` is verified in the gates |
| PostgreSQL via Docker Compose (`docker-compose.yml`) or the one-command demo | **Supported** | pgvector image; operational, proof and corpus databases; documented explicit migrations |
| Frontend | **Supported** as a built static bundle (`web/`: `npm ci && npm run build`) served by Vite in development | No separate package in V1 |
| Node daemon | **Supported** via the same package (`pados node serve --config …`); service templates in `deploy/` | No separate binary |
| Native single-file CLI | Deferred | — |
| Full-stack Docker image | Deferred (post-alpha) | — |
| Kubernetes | **Not planned** | — |

**Optional extras (future).** `pados[node-security]` would bring the asymmetric identity provider with `cryptography`. It is not required for Core.
