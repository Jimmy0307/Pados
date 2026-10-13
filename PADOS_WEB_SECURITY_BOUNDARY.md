# PADOS Web Control Center Security Boundary

## In scope

This surface is a local operator view for a single PADOS workstation. It is designed for localhost use and provides read-only database discovery, project/memory inspection, run/event inspection, and system diagnostics.

## Explicit protections

- PostgreSQL is not exposed to the browser. The browser only calls FastAPI JSON routes.
- The launcher binds backend and frontend to `127.0.0.1` and rejects non-local API clients.
- No database password, API key, connection string or control token is shipped to the frontend.
- No raw SQL console is exposed.
- No `UPDATE`, `DELETE`, `DROP`, `TRUNCATE`, migration, or schema reset route exists in the web API.
- Table and column names are validated against live `public` metadata before use.
- Page size is capped at 100 rows; row preview uses SQLAlchemy expressions and bounded filters.
- Secret-like field names are redacted and binary values are represented as `[BINARY]`.
- Database failures return generic availability errors; backend logs are separate from browser responses.
- Existing mutation-capable `/control/*` routes remain behind their existing control-token boundary and are not called by this first web slice.
- The Control Center is mounted in the same React root as Corpus Operator and has no mutation actions; the accepted authorization and ingest flow remains the only browser write surface.

## Known limits

- This is a local operator surface, not a multi-user or internet-facing administration console.
- The web API deliberately does not provide project creation or operation triggering until a separate mutation contract and confirmation flow are approved.
- Local model availability is reported as `UNKNOWN` unless an existing capability contract is added.
- Exact row counts are opt-in because they can be expensive on large tables.
