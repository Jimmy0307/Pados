# CONSTITUTION

## Truth Sources

- Source code truth: Git repository.
- Operational state truth: PostgreSQL.
- Semantic knowledge truth: version-controlled Markdown Wiki.
- Artifact truth: immutable SHA-256 hashes.

## Governance

- Direct main writes are disabled.
- Automatic merge is disabled.
- Network access is denied by default except localhost services required by the harness.
- Destructive commands are denied unless scoped, reviewed, and explicitly approved.
- Secret access is denied.
- Tests are required for sprint acceptance.
- Independent review is required before release gates.
- Human approval is required before merge.

## Resource Limits

- Local LLM concurrency: 1.
- Cloud LLM concurrency: 2.
- Deterministic workers: 4.
- Active worktrees: 2.
- Maximum fixer cycles: 3.

