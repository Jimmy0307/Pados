# Runtime lane gate (OP-002)

Contract: [`PADOS_SERVICE_ENGINEERING_STANDARD.md`](../PADOS_SERVICE_ENGINEERING_STANDARD.md) §9 (release rule). Code:
`src/pados/release/runtime_lanes.py`, `scripts/release/runtime_lane_gate.py`. Registry:
`scripts/release/runtime_lanes.v1.json`. Source reader: `src/pados/release/runtime_lane_sources.py`. Index tool:
`src/pados/release/runtime_lane_index.py`, `scripts/release/runtime_lane_index.py`. Tests:
`tests/release_supply_chain/test_op002_runtime_lane_gate.py`, `tests/release_supply_chain/test_op002_runtime_lane_index.py`,
`tests/release_supply_chain/test_op002_lane_source_binding.py`, `tests/release_supply_chain/test_op002_lane_sources.py`.

The gate interprets evidence and nothing else. It grants no approval, acceptance or release decision (every report
says `semantic_authority: NONE`). `promotion_check.py`, the approval paths and the WebAuthn/CAS acceptance path stay
authoritative. A green lane report is never a merge, a release or an acceptance.

## Lanes and targets

A **lane** is one runtime path that can be validated. The registry gives each lane:
- its platform;
- how long its evidence stays fresh;
- what it claims (`claims`) and what it explicitly does not cover (`does_not_cover`);
- for lanes that are not validated today, its documented `known_status`;
- its `source`: the one runner record format that can make it PASS, and the complete list of checks that record
  must contain (`required_checks`). A lane without a `source` can never PASS.

`known_status` is informational and never counts as evidence. The `source` binding lives only in the registry,
which is reviewed with the code; neither an evidence index nor an index spec can name, narrow or replace it.

| Lane | Source format | Required checks |
|---|---|---|
| `windows-native-e2e`, `vmware-linux-e2e` | `e2e-result` (`E2E_RESULT.json`) | `verdict` PASS and every step PASS; the platform string must match (a WSL kernel is refused) |
| `windows-ci-gates` | `gate-summary` (`run_gates.ps1`, which must record its `platform`) | the 18 CI gates, from `ci_policy` to `npm_cli` |
| `windows-full-suite` | `gate-summary` of its own run (`run_gates.ps1 -Only full_pytest`) | `full_pytest` |
| `vmware-linux-ci-gates` | `vm-summary` (`virt` must be `vmware`) | the 19 chain gates, including `full_pytest` and `integration_pg` |
| `postgres-integration` | `postgres-gate` (`postgres-integration.json`, schema v1) | all six suites; each suite report beside it is re-hashed and must be PASS for the same commit with no skips and at least the registry's `min_passed` tests (kept equal to the runner's `MIN_TESTS` by a test) |
| `github-ci` | `github-check-runs` (the unmodified `GET /repos/{owner}/{repo}/commits/{sha}/check-runs` response) | the seven required checks; every run must belong to `Jimmy0307/Pados` (a fork shares the commit, not the repository), be from the `github-actions` app (slug and id 15368), be completed and `success` |
| model, REA, OpenMAIC, AI-Scientist-v3 lanes | none | can never PASS today |

A **target** names the lanes it requires:

| Target | Required lanes |
|---|---|
| `local-supervised-dogfood` (the RC2 dogfood boundary, not a production claim) | `windows-native-e2e`, `vmware-linux-e2e`, `windows-ci-gates`, `vmware-linux-ci-gates`, `postgres-integration`, `github-ci` |
| `production` | All of the above, plus `windows-full-suite`, `local-model-measured-execution`, `local-model-governed-acceptance`, `rea-runtime`, `openmaic-runtime`, `ai-scientist-v3-runtime` |

Documented limits that stay visible until real evidence exists:
- `local-model-measured-execution` proves only the bounded path PR #19 ran: Stage B M2 → M2.1/M3 → M4.1 on the
  pinned qwen3:8b. It does not cover model output entering a ResultCandidate or the CAS, the REA, OpenMAIC or
  AI-Scientist-v3 runtimes, other models or runtimes, or hardware attestation.
- `local-model-governed-acceptance` is `NOT_VALIDATED`. The path does not exist by design (R19-6), and no other lane
  satisfies it.
- `rea-runtime` and `openmaic-runtime` (PR #16) and `ai-scientist-v3-runtime` (PR #17, draft) are `NOT_VALIDATED`.
- `windows-full-suite` is documented as `FAIL` (R19-3). A run whose failures match the baseline is recorded as `FAIL`
  with its comparison, never as `PASS`.

## Evidence records

An evidence index (`pados.runtime-lane-evidence.v1`) holds at most one record per lane:

```json
{"lane_id": "vmware-linux-e2e", "status": "PASS", "commit": "<40-hex>", "platform": "linux",
 "command": "vm_online_chain.sh <sha> <branch>", "exit_code": 0,
 "evidence_path": "vmware-online-<sha>/online-<sha>-out/e2e/E2E_RESULT.json",
 "evidence_sha256": "<64-hex>", "recorded_at": "2026-11-01T14:23:25+00:00", "notes": ""}
```

`PASS` and `FAIL` need an exit code, an evidence path (relative to the evidence root) and its SHA-256. `SKIPPED` and
`NOT_VALIDATED` say why in `notes`. Evidence indexes stay with the machine-local evidence under `runtime/artifacts/`;
they are not committed.

## Building the index

Do not write an evidence index by hand. Write a spec (`pados.runtime-lane-spec.v1`) that says what ran and where
the runner's own record is, and let `runtime_lane_index.py build` read the rest from that record:

```json
{"schema_version": "pados.runtime-lane-spec.v1", "commit": "<40-hex>", "lanes": [
  {"lane_id": "windows-ci-gates", "platform": "windows", "command": "run_gates.ps1",
   "evidence_path": "windows-<sha>/gate-summary.json"},
  {"lane_id": "windows-native-e2e", "platform": "windows", "command": "local_dogfood_e2e.py",
   "evidence_path": "e2e-windows-<sha>/E2E_RESULT.json"},
  {"lane_id": "postgres-integration", "platform": "windows", "command": "postgres_integration_gate.py --recreate",
   "evidence_path": "windows-postgres-gate-<sha>/postgres-integration.json"},
  {"lane_id": "github-ci", "platform": "github", "command": "gh api repos/<o>/<r>/commits/<sha>/check-runs?per_page=100",
   "evidence_path": "github-checks-<sha>.json"},
  {"lane_id": "windows-full-suite", "platform": "windows", "command": "pytest -q tests", "exit_code": 1,
   "evidence_path": "windows-<sha>/full_pytest_baseline_compare.json",
   "notes": "126 failures, identical to the recorded Windows baseline"},
  {"lane_id": "rea-runtime", "platform": "any", "command": "not run", "declare": "NOT_VALIDATED",
   "notes": "no real REA runtime available"}]}
```

- **A PASS comes only from the registry-bound runner record.** The spec names the evidence file. The builder reads
  it with `runtime_lane_sources.read_source` under the lane's registry `source`: the format, the complete
  `required_checks`, the commit, the platform and the finish time. A spec that names `source` or `gates` is refused
  (`RUNTIME_LANE_SPEC_INVALID`), so a spec cannot keep only the gates that passed.
- The build is refused for a record for another commit (`RUNTIME_LANE_SOURCE_COMMIT_MISMATCH`), a missing required
  check (`RUNTIME_LANE_SOURCE_GATE_MISSING`), a record of the wrong shape (`RUNTIME_LANE_SOURCE_FORMAT_INVALID`) or a
  platform mismatch (`RUNTIME_LANE_SOURCE_PLATFORM_MISMATCH`).
- Any failing required check makes the lane FAIL. A spec-typed `exit_code: 0` is refused
  (`RUNTIME_LANE_PASS_NEEDS_MACHINE_SOURCE`). A non-zero typed exit code gives FAIL, and no exit code and no evidence
  give NOT_VALIDATED. A lane without a registry source is NOT_VALIDATED whatever its evidence says.
- A spec may only downgrade a lane, to `SKIPPED` or `NOT_VALIDATED`, with a reason of at least ten characters.
- The tool computes the SHA-256 itself. `recorded_at` is the record's finish time (its modification time when it has
  none).
- The build is refused (exit 2) if a ran lane's evidence file is missing, if a path escapes the root, if one file
  backs two lanes, or if a lane id is not in the registry.
- `verify` re-hashes every referenced file and exits 1 on a mismatch or a missing file.

Every record must carry its own finish time (`finished`, or each check run's `completed_at`); a record without one is
refused (`SOURCE_FORMAT_INVALID`), and the file's modification time never stands in for it, so copying or touching a
record cannot make it fresh. The finish time inside the record is still only as trustworthy as the record's bytes.

The PostgreSQL record must be the schema v1 aggregate the gate writes from this commit on. Older aggregates (status
and suite names only) carry no commit, so they are refused (`SOURCE_FORMAT_INVALID`); run the gate again. The GitHub
record must be the raw API response for the commit, not a summary; a response that holds fewer runs than its
`total_count` (a truncated page) is refused.

## How a lane is judged

`runtime_lane_gate.py` (report and `--claim`) does not trust the index's PASS, exit code or `recorded_at`. For a PASS
record it checks the index fields, then re-reads the evidence file under the lane's registry `source`, exactly as the
builder does. The runner record's own verdict, commit, platform and finish time decide. A required lane counts only
as `PASS` / `VERIFIED`. Every other outcome blocks the target:

| Outcome | Status | Reason |
|---|---|---|
| No record | NOT_VALIDATED | `NO_EVIDENCE_RECORD` |
| Record says SKIPPED / NOT_VALIDATED / FAIL | the same | `LANE_SKIPPED` / `LANE_NOT_VALIDATED` / `LANE_FAILED` |
| Lane has no machine source in the registry | NOT_VALIDATED | `LANE_HAS_NO_MACHINE_SOURCE` |
| Recorded on another platform | FAIL | `EVIDENCE_PLATFORM_MISMATCH` |
| Recorded for another commit | NOT_VALIDATED | `EVIDENCE_COMMIT_MISMATCH` |
| PASS with a non-zero exit code | FAIL | `PASS_WITH_NONZERO_EXIT` |
| Evidence path escapes the evidence root | FAIL | `EVIDENCE_PATH_OUTSIDE_ROOT` |
| Evidence file absent | FAIL | `EVIDENCE_FILE_MISSING` |
| SHA-256 differs | FAIL | `EVIDENCE_HASH_MISMATCH` |
| The same evidence file cited for two lanes | FAIL (both) | `EVIDENCE_REUSED_ACROSS_LANES` |
| The runner record is for another commit, or a GitHub check is still running | NOT_VALIDATED | `SOURCE_COMMIT_MISMATCH` / `SOURCE_CHECK_INCOMPLETE` |
| The runner record is not of the lane's format, or lacks its finish time or platform | FAIL | `SOURCE_FORMAT_INVALID` |
| A GitHub record from another repository | FAIL | `SOURCE_REPOSITORY_MISMATCH` |
| A required check is absent from the record | FAIL | `SOURCE_GATE_MISSING` |
| A PostgreSQL suite report is missing or was edited | FAIL | `SOURCE_SUITE_REPORT_MISSING` / `SOURCE_SUITE_REPORT_HASH_MISMATCH` |
| The record's platform (or hypervisor) is not the lane's | FAIL | `SOURCE_PLATFORM_MISMATCH` |
| The record itself does not say PASS for every required check | FAIL | `SOURCE_RECORD_NOT_PASS` |
| The record finished longer ago than `max_age_days` | NOT_VALIDATED | `EVIDENCE_STALE` |
| The record finished in the future (beyond 5 minutes of clock skew) | FAIL | `EVIDENCE_FROM_THE_FUTURE` |
| A record for a lane the registry does not know | blocks the target | `UNKNOWN_LANE` |

Evidence binds to the exact commit, so a lane measured at an earlier commit, a model measurement included, has to be
run again for the commit being judged.

## What the gate does not prove

The gate and the index prove *binding*: these bytes, unchanged, are a record in the lane's registered format that
says PASS for every required check, for this commit and platform, recently enough. They do not authenticate who
wrote the bytes, and they do not prove that the evidence is true, complete or sufficient. A log that says PASS for the wrong test set, a gate
script that skips a check, or an E2E run against a mocked dependency all bind cleanly. Content truth stays with human
review of the evidence and the existing approval and acceptance paths. A lane's `claims` and `does_not_cover` in the
registry say what a verified lane may be quoted for; nothing outside `claims` may be quoted.

## Commands

```text
# Report: writes per-lane status and exits 0; it records and never claims.
python scripts/release/runtime_lane_gate.py --target production --out gates/runtime-lanes-production.json

# Evaluate machine-local evidence for one commit.
python scripts/release/runtime_lane_gate.py --target local-supervised-dogfood --commit <sha> \
  --evidence <evidence-dir>/runtime-lane-evidence-<sha>.json --out <evidence-dir>/runtime-readiness.json

# Build and verify an index from a spec (see "Building the index").
python scripts/release/runtime_lane_index.py build <evidence-dir>/lane-spec-<sha>.json   --out <evidence-dir>/runtime-lane-evidence-<sha>.json
python scripts/release/runtime_lane_index.py verify <evidence-dir>/runtime-lane-evidence-<sha>.json

# Claim: exits 1 with RUNTIME_READINESS_NOT_PROVEN and the blockers unless every required lane is VERIFIED.
python scripts/release/runtime_lane_gate.py --target production --claim --out <file>
```

A malformed registry or evidence index exits 2 (`RUNTIME_LANE_REGISTRY_INVALID` / `RUNTIME_LANE_EVIDENCE_INVALID`).

CI (linux job) runs the report for `production` with no evidence, and uploads it with the gate reports. Every required
lane there reads `NOT_VALIDATED` and readiness is `NOT_READY`, which is the honest CI state. No wording may describe
PADOS as production-ready while `--claim --target production` fails.
