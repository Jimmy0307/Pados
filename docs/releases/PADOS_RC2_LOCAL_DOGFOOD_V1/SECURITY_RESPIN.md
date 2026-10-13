# PADOS_RC2_LOCAL_DOGFOOD_V1: dependency security re-spin

Enabling the GitHub Dependency graph turned dependency review from an environment blocker into a real, required
check. It failed twice on runtime dependencies that ship inside both runtime packages. Both were fixed before the
freeze, as two narrowly scoped commits.

## 1. anyio 4.14.1 → 4.14.2 (commit `631afa4`)

| Advisory | Severity | Issue | Fixed in |
|---|---|---|---|
| GHSA-82r6-8w77-94w6 | critical | TLSStream IDNA 2003 host name encoding allows TLS certificate spoofing | 4.14.2 |
| GHSA-3w57-8xmc-8v26 | high | `run_process` ignores `extra_groups` | 4.14.2 |

- PADOS does not import anyio directly; it arrives through starlette, httpx and httpcore.
- Pins changed: `requirements.lock`, `packages/scripts/runtime-wheels-win32-x64.txt`,
  `packages/scripts/runtime-wheels-linux-x64.txt`, `scripts/stage_b/requirements-validation.txt`.
- anyio 4.14.2 declares the same dependencies as 4.14.1. Regenerating the win32 lock with
  `build_runtime.py --lock` changed only anyio (37 wheels).

## 2. pypdf 6.14.2 → 6.20.1 and the OCR-01 Configuration migration (commit `dd72831`)

pypdf 6.14.2 was affected by eight high-severity denial-of-service advisories on malicious PDFs:

| Advisory | Fixed in |
|---|---|
| GHSA-qv6h-rv94-w285 | 6.17.0 |
| GHSA-5jq2-8x83-x246 | 6.18.0 |
| GHSA-fp3h-c4fm-7vvf | 6.18.1 |
| GHSA-g9cg-prrw-2r8q | 6.18.1 |
| GHSA-jw7q-gvrg-4vj3 | 6.18.1 |
| GHSA-w23x-9jrw-r45c | 6.19.0 |
| GHSA-php9-fj8v-98fj | 6.19.0 |
| GHSA-v247-6f48-mgcj | 6.19.0 |

Six further medium advisories were below the review threshold and are all fixed by 6.16.1. The GitHub advisory
database lists no advisory for 6.20.1. pypdf 6.20.1 declares the same dependencies as 6.14.2.

### Why a plain version bump was not acceptable

The OCR-01 control (`src/pados/packs/ocr/_pdf_worker.py`) caps PDF decompression at PADOS's budget. Up to 6.14.2 it
did this by lowering nine `pypdf.filters` module constants. From pypdf 6.19 those constants are deprecated and no
longer read by the decoders; only pypdf's own defaults (75 MB) would apply. PADOS's read-back check would still have
passed, so the control would have weakened silently. The existing test
`tests/packs/ocr/test_ocr_cr15c.py::test_decode_limits_are_set_from_the_budget_and_observable` passed on 6.14.2 and
failed on 6.20.1, which confirmed this.

### The migration

- The same nine controls are applied through pypdf's public Configuration API (for example
  `ZLIB_MAX_OUTPUT_LENGTH` → `zlib_maximum_output_length`, `MAX_DECLARED_STREAM_LENGTH` →
  `maximum_declared_stream_length`).
- The policy is unchanged. Each value is still the smaller of pypdf's default and the budget, and a zero budget is
  still refused.
- `decode_budget()` applies the budget only for the duration of each operation (`inspect`, `figures`, `text`),
  reads every value back, and restores the previous settings afterwards.
- pypdf's legacy constant handling is disabled inside that scope, so a changed deprecated constant cannot loosen the
  budget.
- `pyproject.toml` now requires `pypdf>=6.20.1,<7`, because the code needs the Configuration API.

### Evidence

- New tests in `tests/packs/ocr/test_ocr_cr15c.py` prove the following:
  - pypdf itself reports the budget;
  - a 4 MiB stream is refused at a 512 KiB budget, not at pypdf's 75 MB default;
  - a 256 KiB stream still decodes;
  - the budget is restored afterwards and does not leak into another execution context;
  - legacy constants cannot loosen it.
- On Windows, the PDF/OCR suites fail in exactly the same 135 tests before and after the change. OCR worker isolation
  needs POSIX, so these tests are expected to fail there.
- On Linux, PR CI and RC CI ran the full PDF/OCR suites with no skips and no failures.
- In the installed RC runtime, the 512 KiB budget is in force and a decompression bomb is refused with
  `OCR_DECODE_BUDGET_EXCEEDED`. Normal extraction, truncated files and encrypted files behave as before.

## Verdict

| Item | Value |
|---|---|
| anyio | 4.14.2 |
| pypdf | 6.20.1 |
| OCR-01 decode budget | enforced through `pypdf.Configuration`; no legacy limit mutation remains |
| Dependency review | PASS |
| Known blocking advisories | 0 |

Older version numbers still appear in `THIRD_PARTY_NOTICES.md` and `docs/packs/ocr/OCR_CAPABILITY_PACK_V1.md`. Both
are documentation, not release inputs, and are tracked in `KNOWN_ISSUES.md`.
