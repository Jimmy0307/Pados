# Deterministic CI vs physical hardware acceptance

CI is not PADOS authority (see `SECURE_CICD_V1.md`). This page separates two kinds of evidence. They are
never substituted for each other.

| | Deterministic CI | Physical hardware acceptance |
|---|---|---|
| Where | GitHub-hosted ephemeral runners (`ubuntu-latest`, `windows-latest`, `macos-latest`) | The reference node, which holds the measured device, runtime and model artifact |
| What it proves | Code behaves as specified: unit and integration tests, security and architecture boundaries, ruff, mypy strict, packaging, public-export hygiene, model-independent planner tests, and the measured-execution contract against a **fake** runtime | A real task ran through the governed chain on the measured hardware, under the measured execution configuration |
| Model / GPU | None. The fake runtime's numbers are synthetic and never evidence about a model | The pinned artifact (manifest digest), on the profiled device |
| Result | Workflow status | A machine-captured archive, pinned by SHA256, verified **independently** on another host |

## Rules

1. CI never claims hardware acceptance. A CI job never mocks the reference GPU as if it were real. Rehearsal
   archives are labelled `REHEARSAL_NOT_EVIDENCE`, and the verifier refuses them in live mode.
2. Hardware acceptance is an external artifact. The operator runs `scripts/stage_b/windows_m4_1_dogfood.py`
   on the reference node. It produces an archive with internal `SHA256SUMS`. The archive hash is recorded,
   and `scripts/stage_b/m4_1_verify_dogfood.py` re-checks the archive on a different host without trusting
   the archive's own conclusions. That includes re-deriving the bound measured execution condition from the
   archived catalog.
3. The archive itself stays outside Git, in internal handoff storage. Its hash, the result JSON
   (byte-exact) and the independent verifier output are tracked as internal evidence. The public export
   carries the verifier and the contracts, not the evidence.
4. A hardware acceptance is valid only for the code commit and tree it records. A later change to the gated
   path needs a new live run.

## Stage B record

`STAGE_B_LOCAL_INTELLIGENCE_V1=ACCEPTED`. The live archive SHA256 is
`f81036e83c5d90a43f6c837e040aa03929d5fffae51c26239411aafb71ec6cb3` (code `ed9ef89`). The Linux independent
verifier passed 31 of 31 checks. CI covers the same contract deterministically:
`tests/local_intelligence/test_measured_execution_contract.py` and the live-chain rehearsal.
