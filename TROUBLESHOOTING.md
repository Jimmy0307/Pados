# TROUBLESHOOTING

## Docker is unavailable

Run `docker --version` and `docker compose version`. P0 requires Docker Compose for PostgreSQL + pgvector validation.

## Database health fails

Run `docker compose ps` and inspect `docker compose logs postgres`. Confirm the configured port in `.env.example` is available.

## CLI is unavailable

Use the project virtual environment:

```powershell
.\.venv\Scripts\pados doctor
```

