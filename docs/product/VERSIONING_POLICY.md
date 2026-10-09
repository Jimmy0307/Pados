# Versioning policy

Versions are independent axes. Public version numbers are **not** tied to internal milestone names (P6.x, "V1 phases"). `pados version --json` prints all of them (`pados.versions`).

| Axis | Current | Scheme | Bumps when |
|---|---|---|---|
| Product (`pados` package) | `0.1.0a1` (channel `alpha`) | SemVer; PEP 440 pre-release suffixes `aN`/`bN`/`rcN` | Any release |
| Compute Node protocol | `pados.compute-node.protocol.v1` | Integer major in the identifier | Any breaking wire change (new endpoints and optional fields are non-breaking) |
| Node daemon runtime | `pados-lcn-1.0.0` | SemVer | Daemon releases |
| Capability manifest schema | `pados.enterprise.capability-manifest.v1` | Integer major | Breaking field or semantics change |
| Enterprise retrieval contract | `pados.enterprise.retrieval.v1` | Integer major | Breaking request or response change |
| ExecutionReceipt schema | `schema_version: "1"` | Integer | Any change to hashed fields (receipts are integrity-bound) |
| Node execution receipt | `pados.compute-node.execution-receipt.v1` | Integer major | Any change to hashed fields |
| Database | Alembic revisions: operational `20261007_0017`; proof `p6_21p_0015` on formal main heads `20260730_0011` + `20260808_0013` | Forward-only revisions | Schema changes. Revision ids are internal; the upgrade procedure is public |

**Rules**

- A product release declares the protocol and schema majors it speaks.
- Protocol and schema majors can change only in a product minor release (in 0.x) or a major release (in ≥1.0).
