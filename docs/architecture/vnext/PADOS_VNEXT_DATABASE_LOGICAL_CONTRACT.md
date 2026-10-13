# PADOS vNext Database Logical Contract

Status: `PROPOSED`; this document intentionally contains no SQL or migration instructions.

| Aggregate | Owner / transaction boundary | Mutability and history | Storage and audit rule |
| --- | --- | --- | --- |
| Project / Workspace | Project context | Mutable registered attributes; boundary changes versioned | Root, storage and ACL changes audited |
| Task | Task and Workflow context | Current state mutable by command; state events append-only | Every transition has actor, policy and correlation ID |
| Execution | Execution Runtime context | Current status mutable by command; steps/events append-only | Adapter observations are linked, not authoritative state |
| Approval | Approval context | Decision immutable; revocation/supersession creates new record | Scope hash, approver, expiry and policy version required |
| Validation | Artifact/Validation context | Run/result immutable after finalization | Command, exit code, logs, evidence links required |
| Evidence | Artifact/Validation context | Append-only, immutable content hash | May reference artifacts, logs, validation and approvals |
| Release | Release context | Candidate mutable before seal; manifest immutable after seal | Manifest hash, approval and rollback metadata required |
| Artifact | Artifact context | Artifact version/hash immutable; metadata superseded by version | Bodies outside PostgreSQL; database owns URI, hash, lineage and retention |
| Source / Context Pack | Context Service | Sources versioned; Context Pack immutable | Pack persists ranked selection, exclusions, policy and source lineage |
| Provider / token source | Provider Governance | Configuration versioned; usage observations append-only | Credentials only as secret reference |
| Knowledge / memory metadata | Knowledge Memory Phase 2 | Provider-specific indexes external; metadata versioned | Cannot own formal project/task/execution state |
| Audit / Outbox | Audit context | Append-only | Transactional outbox records integration events in same commit as source change |

## Isolation and retention

- Every governed record has a project scope unless explicitly system-scoped.
- Query paths apply project scope before retrieval ranking or storage access.
- Retention policies classify artifacts, logs, evidence and external provider observations independently.
- Deletion requests preserve legal/audit tombstones and lineage where policy requires; they do not silently rewrite immutable history.
