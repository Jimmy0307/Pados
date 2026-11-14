# Owner decision dossier: PR #20 → #21 → #22 → #24 → #25 (2026-11-14)

This dossier lists what each PR needs before it can merge, read from GitHub and machine evidence on 2026-11-14.
It decides nothing: merging requires owner review and repository approval. Author self-review is not an
independent approval.

Canonical `main`: `69716ed91237d0bc9ceba41404aedec42f4a7c3e` (PR #19 merge). Branch protection on `main`:

- seven required checks;
- `strict` (the branch must be up to date with `main`);
- 0 required approving reviews, but stale reviews are dismissed;
- `enforce_admins`;
- no force pushes or deletions.

Every PR below is a **draft** with **0 reviews**.

## Shared facts

- **Code scanning.** There are 7 open alerts, all on `main` and all predating this stack:
  - path-injection ×3 in `product_loop`;
  - clear-text storage and logging ×2;
  - stack-trace exposure ×1;
  - polynomial ReDoS ×1 in `packages/cli`.

  No PR's merge ref has an open alert, so no PR in the stack introduces one. Triaging these is separate work.
- **Dependabot.** There are 27 open alerts: 3 critical, 12 high, 10 medium, 2 low (R19-8). They predate the
  stack, and no dependency upgrade is authorized.
- **Secret scanning.** 0 open alerts.
- **Readiness.** Production is NOT_READY on every head. Its required lanes stay NOT_VALIDATED:
  - the model lanes;
  - REA, OpenMAIC and AI-Scientist;
  - `windows-full-suite`, which fails by design: 163 failures, identical to the baseline (OCR isolation and
    operator issuance fail closed on win32, R19-3).

## PR #20: Engineering operability remediation (head `dd8dcf3`, base `main`)

| | |
| --- | --- |
| CI on head | 11 of 11 checks SUCCESS (pr-ci linux/windows/macos, security) |
| Lane gate on head | local-supervised-dogfood **READY**; production NOT_READY |
| Merge blockers | 1. The PR body asks for owner review ("draft, not for merge yet") and has an unchecked owner-review item. 2. It is a draft. 3. A rebase or merge refresh is needed if `main` moves (strict). |
| Documented non-production residuals | OP-004 partial (deadline limits listed); OP-008 exporter and dashboards; OP-009 schema v2; C07, C12, C17 health contracts; C08 contract record; R19-1, R19-2, R19-3, R19-6, R19-7 and R19-8 |
| Owner decisions | Accept the register's remaining items; OP-009 schema v2; the R19-1 approval-epoch design |

## PR #21: Local single-user usability (head `034fce6`, base #20)

| | |
| --- | --- |
| CI on head | 11 of 11 SUCCESS |
| Lane gate on head | local-supervised-dogfood **NOT_READY**: windows-ci-gates FAIL at this head (the R19-3 timing tests; fixed in #22) |
| Merge blockers | 1. #20 must merge first. 2. The owner review requested in the body. 3. Its own head is not dogfood-READY; merging it alone would put a known timing failure on `main` until #22 lands. |
| Documented non-production residuals | Usability gaps listed in the body ("Still not usable"); the redaction pattern cost ~0.6 s/MiB in one shape (R19-3) |
| Owner decisions | Merge #21 and #22 together, or accept #21's timing failure for one step; the timing-test policy (R19-3) |

## PR #22: Runtime hardening (head `205543a`, base #21)

| | |
| --- | --- |
| CI on head | 11 of 11 SUCCESS |
| Lane gate on head | local-supervised-dogfood **READY** (Windows and VMware, 2026-11-11); production NOT_READY |
| Merge blockers | 1. #20 and #21 must merge first. 2. Owner review (an independent security review returned APPROVE, but that is not a GitHub approval). |
| Documented non-production residuals | C07, C12 and C17 health contracts; Windows Job Object containment (135 Windows failures); Windows operator issuance (28 failures) |
| Owner decisions | Listed in the PR body ("Needs an owner decision") |

## PR #24: Document intelligence (head `74df247`, base #22)

| | |
| --- | --- |
| CI on head | 11 of 11 SUCCESS; GitHub evidence bundle `github-actions-74df247.json` |
| Windows on head | All 18 CI gates, E2E, the PostgreSQL gate, Pester and Playwright PASS. Full pytest: 4901 tests, 163 failures, identical to the baseline. |
| VMware on head | The unmodified `vm_online_chain.sh`, offline (see `docs/operations/VMWARE_OFFLINE_VALIDATION.md`), run `-r3`: 17 of 19 gates exit 0. **`full_pytest`, `integration_pg` and the E2E step FAIL** because the VM's `docker0` bridge has no IPv4 address, so no PostgreSQL container is reachable (environment). A targeted offline run passed 1193 of 1193. |
| Lane gate on head | local-supervised-dogfood **NOT_READY**: `vmware-linux-e2e` and `vmware-linux-ci-gates` FAIL (environment); production NOT_READY |
| Merge blockers | 1. #20, #21 and #22 first. 2. Owner review. 3. A VMware lane PASS at the head: owner action `sudo systemctl restart docker` on the VM, then rerun. |
| Documented non-production residuals | DI-1 … DI-10 (`docs/audits/PADOS_OPERABILITY_RESIDUAL_REGISTER.md`) |
| Owner decisions | DI-1 (in-process vs worker), DI-4 (OCR deployment), DI-7 (a real corpus before ACTIVE rerank), DI-10 (third-party ONNX conversion provenance) |

## PR #25: Document intelligence hardening (base #24)

| | |
| --- | --- |
| Content | DI-2 private verified model copy; DI-1 opt-in subprocess model worker; the DI-9 artifact read fix (**touches Core** `capability/artifacts.py`, semantics unchanged); the scanned-PDF OCR candidate; docs and dashboard |
| Evidence | Windows: the document-intelligence and capability suites (failures identical to the baseline). VMware fresh offline venv at `f699344`: 1216 of 1216 passed. **Live scanned-PDF OCR** in a locked-down container: 2 pages, char similarity 1.00 on clean renders. CI: see the PR. |
| Merge blockers | Everything above, plus owner review of the Core touch (DI-9) and of the worker as a candidate boundary |
| Owner decisions | Make the worker the default? Adopt Job Object or cgroup memory limits and an OS network policy for it? Accept `pypdfium2` and a Tesseract worker image for OCR? |

## Recommended merge order (owner's call)

1. #20.
2. #21 and #22 together, so `main` never carries #21's timing failure.
3. Restore the VM's Docker bridge, rerun the VMware lanes on #24's head, then #24.
4. #25 after owner review of its Core touch and the worker design.

Each step needs fresh CI on the then-current head, because `strict` requires the branch to be up to date with
`main`. Evidence stays tied to the exact SHA it was recorded at.
