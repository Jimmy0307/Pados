# Runtime lane gate (OP-002)

Contract: [`PADOS_SERVICE_ENGINEERING_STANDARD.md`](../PADOS_SERVICE_ENGINEERING_STANDARD.md) §9 (release rule). Code:
`src/pados/release/runtime_lanes.py`, `scripts/release/runtime_lane_gate.py`. Registry:
`scripts/release/runtime_lanes.v1.json`. Index tool: `src/pados/release/runtime_lane_index.py`, `scripts/release/runtime_lane_index.py`. Tests:
`tests/release_supply_chain/test_op002_runtime_lane_gate.py`, `tests/release_supply_chain/test_op002_runtime_lane_index.py`.

The gate interprets evidence and nothing else. It grants no approval, acceptance or release decision (every report
says `semantic_authority: NONE`). `promotion_check.py`, the approval paths and the WebAuthn/CAS acceptance path stay
authoritative. A green lane report is never a merge, a release or an acceptance.

## Lanes and targets

A **lane** is one runtime path that can be validated. The registry gives each lane:
- its platform;
- how long its evidence stays fresh;
- what it claims (`claims`) and what it explicitly does not cover (`does_not_cover`);
- for lanes that are not validated today, its documented `known_status`.

`known_status` is informational and never counts as evidence.

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
   "source": "gate-summary", "gates": ["ci_policy", "ruff", "mypy"], "evidence_path": "windows-<sha>/gate-summary.json"},
  {"lane_id": "vmware-linux-e2e", "platform": "linux", "command": "vm_online_chain.sh <sha> <branch>",
   "source": "vm-summary", "gates": ["e2e"], "evidence_path": "vmware-<sha>/summary.json"},
  {"lane_id": "windows-native-e2e", "platform": "windows", "command": "local_dogfood_e2e.py",
   "source": "e2e-result", "evidence_path": "e2e-windows-<sha>/E2E_RESULT.json"},
  {"lane_id": "windows-full-suite", "platform": "windows", "command": "pytest -q tests", "exit_code": 1,
   "evidence_path": "windows-<sha>/full_pytest.log", "declare": "NOT_VALIDATED",
   "notes": "126 failures, identical to the recorded Windows baseline"},
  {"lane_id": "github-ci", "platform": "github", "command": "ci.yml", "notes": "no CI run for this commit"}]}
```

- **A PASS comes only from a machine-written runner record** (`source`):
  - `gate-summary`: the Windows `run_gates.ps1` summary;
  - `vm-summary`: the VMware chain summary, which must say `virt: vmware`;
  - `e2e-result`: the dogfood `E2E_RESULT.json`, whose platform must match.
- The builder reads the commit, the named gates' exit codes and the finish time from that record. It refuses a record
  for another commit, a missing gate, or a platform mismatch.
- Any non-zero gate makes the lane FAIL. A spec-typed `exit_code: 0` is refused (`RUNTIME_LANE_PASS_NEEDS_MACHINE_SOURCE`).
  A non-zero typed exit code gives FAIL, and no exit code gives NOT_VALIDATED.
- A spec may only downgrade a lane, to `SKIPPED` or `NOT_VALIDATED`, with a reason of at least ten characters.
- The tool computes the SHA-256 itself. `recorded_at` is the record's finish time (its modification time when it has
  none), so staleness counts from when the evidence was produced and copying a file does not refresh it.
- The build is refused (exit 2) if a ran lane's evidence file is missing, if a path escapes the root, if one file
  backs two lanes, or if a lane id is not in the registry.
- `verify` re-hashes every referenced file and exits 1 on a mismatch or a missing file. Run it before reporting
  numbers from an index that has sat on disk.
- A record can still be forged, or come from a runner that checks the wrong thing. See "What the gate does not
  prove".

## How a lane is judged

A required lane counts only as `PASS` / `VERIFIED`. Every other outcome blocks the target:

| Outcome | Status | Reason |
|---|---|---|
| No record | NOT_VALIDATED | `NO_EVIDENCE_RECORD` |
| Record says SKIPPED / NOT_VALIDATED / FAIL | the same | `LANE_SKIPPED` / `LANE_NOT_VALIDATED` / `LANE_FAILED` |
| Recorded on another platform | FAIL | `EVIDENCE_PLATFORM_MISMATCH` |
| Recorded for another commit | NOT_VALIDATED | `EVIDENCE_COMMIT_MISMATCH` |
| Older than the lane's `max_age_days` | NOT_VALIDATED | `EVIDENCE_STALE` |
| Dated in the future (beyond 5 minutes of clock skew) | FAIL | `EVIDENCE_FROM_THE_FUTURE` |
| PASS with a non-zero exit code | FAIL | `PASS_WITH_NONZERO_EXIT` |
| Evidence path escapes the evidence root | FAIL | `EVIDENCE_PATH_OUTSIDE_ROOT` |
| Evidence file absent | FAIL | `EVIDENCE_FILE_MISSING` |
| SHA-256 differs | FAIL | `EVIDENCE_HASH_MISMATCH` |
| The same evidence file cited for two lanes | FAIL (both) | `EVIDENCE_REUSED_ACROSS_LANES` |
| A record for a lane the registry does not know | blocks the target | `UNKNOWN_LANE` |

Evidence binds to the exact commit, so a lane measured at an earlier commit, a model measurement included, has to be
run again for the commit being judged.

## What the gate does not prove

The gate and the index prove *binding*: this commit, this platform, this command, this exit code, these bytes. They
do not prove that the evidence is true, complete or sufficient. A log that says PASS for the wrong test set, a gate
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
