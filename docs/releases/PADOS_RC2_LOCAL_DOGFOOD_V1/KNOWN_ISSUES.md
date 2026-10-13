# PADOS_RC2_LOCAL_DOGFOOD_V1: known issues

The freeze was released with the issues below open. None was hidden, downgraded or marked resolved to make the
freeze pass. Each P1 is contained only by the operating rules in `DOGFOOD_BOUNDARY.md`.

## Open P1 defects (P0: none)

| ID | Area | Defect | Containment at this freeze |
|---|---|---|---|
| ARCH-1 / SEC-1 | Product loop | Cloud (Codex) dispatch and a CAS commit run on a self-asserted `authorized` flag. Any APPROVED approval is accepted, whatever its subject. | The product-loop service refuses to start without a proof/product database. Never configure one during dogfood. |
| SEC-2 | Product loop | Project context is sent to a cloud worker with no data-classification gate. It becomes P0 if any project is classified RESTRICTED. | Same as ARCH-1: the product loop is unreachable. |
| SEC-3 | TaskLoop | pause, resume and cancel have no status predicates, so terminal or WAITING_HUMAN loops can be revived. | Manual supervision. Never pause or resume a finished loop. |
| SEC-4 | Approvals | Approvals are never revoked, expired or consumed. Request and project status are not checked at route time. | A single local token holder. D1 binds each approval to its subject. Keep approval subjects narrow. |
| SEC-7 | Control token | The public default control-token hash is accepted on the developer (Python) start path. | The npm install generates a random token. Do not start PADOS through the developer `pados` command. |
| ARCH-5 | API | `/memory/*` and `/context/assemble` are unauthenticated. | Loopback binding only. Residual risk: a local process could write candidate memory; DNS rebinding is unconfirmed. Neither can trigger execution. |
| EXEC-1 | Execution | Jobs routed to Codex/Claude, or to deterministic tools without a command, have no executor and stay ROUTED until they time out. | Approvals list only the deterministic and local Ollama worker types. |
| INST-1 | Installer | Re-running `pados init` with a newer CLI migrates and activates a runtime with no consent. | Fresh installs only. Upgrade only through `pados update`. Never point `init` at an existing database. |

These findings come from the RC2 repository review; finding IDs refer to that review. The P2 items most likely to
affect dogfood are:

| ID | Issue |
|---|---|
| EXEC-2, EXEC-4, EXEC-5, SEC-10 | TaskLoop sink states and retry behaviour; a route refusal leaves a loop RUNNING |
| MODEL-1 | The mock provider is the default in the `local` environment |
| UX-2 | Forced shutdown on Windows |
| INST-2, INST-4 | Reconfiguration and partial-migration reporting |
| STOR-4, STOR-5 | Absolute proof object paths; backup covers one database |
| SEC-9 | Model calls run inside the routing transaction |

## Open review threads on PR #2: 26, deliberately left unresolved

| Source | Count | Notes |
|---|---|---|
| `chatgpt-codex-connector` automated review (P1/P2 labels) | 19 | Most overlap known review findings (for example INST-5, INST-13, STOR-6, DB-5). |
| GitHub code scanning (CodeQL) | 7 | 6 high and 1 medium; listed below |

The threads stay open as a register; they were not resolved to unblock the merge. The `main` branch protection does
not require conversation resolution (`DOGFOOD_BOUNDARY.md`, "Repository controls").

The CodeQL alerts:
- path injection: `src/pados/product_loop/service.py` (two locations) and `src/pados/product_loop/authorization.py`;
- clear-text storage of sensitive information, in `src/pados/cli/main.py` (INST-5);
- clear-text logging, in `src/pados/machines/bootstrap.py`;
- polynomial ReDoS: `packages/cli/lib/home.js`;
- stack-trace exposure: `src/pados/api/app.py`.

The CodeQL *results* check is not a required check. The required CodeQL analysis jobs pass.

## Documentation drift at the freeze

| Document | States | Actual |
|---|---|---|
| `docs/supply_chain/MAIN_BRANCH_PROTECTION.md` | 1 approval, linear history, conversation resolution | 0 approvals, no linear history, conversation resolution not required |
| `THIRD_PARTY_NOTICES.md`, `docs/packs/ocr/OCR_CAPABILITY_PACK_V1.md` | anyio 4.14.1, pypdf 6.14.2 | anyio 4.14.2, pypdf 6.20.1. The shipped runtime's notices file is generated at build time. |

## Other follow-ups

- Frontend development dependencies have open advisories that do not ship (`NPM_AUDIT_RELEASE_IMPACT_TRIAGE.md`).
- The synthetic `0.1.0-alpha.99` upgrade-test tarballs are uploaded with the linux-x64 RC packages. Promotion must
  exclude them, or packaging should stop uploading them.
- Retrieval quality: lexical retrieval measured 5/10 on the RC2 gold set on the experimental, unmerged branch
  `fix/pados-rq1-1-retrieval-closure` (RC2 baseline 1/10). By decision, this is not a release blocker.
