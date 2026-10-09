#!/usr/bin/env bash
# Local liveness check: an unauthenticated request must be refused (401).
# Authoritative health comes only from the PADOS-side `pados node probe` (identity proof + manifest).
set -euo pipefail
ENDPOINT=${1:-http://127.0.0.1:8765}
code=$(curl -s -o /dev/null -w "%{http_code}" "$ENDPOINT/health" || true)
if [ "$code" = "401" ]; then echo "NODE_REACHABLE_AUTH_REQUIRED"; else echo "NODE_UNEXPECTED_STATUS:$code"; exit 1; fi
