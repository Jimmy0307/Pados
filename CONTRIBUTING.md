# Contributing

Thanks for contributing to PADOS. Keep intent, policy, and semantic authority inside PADOS; treat provider and worker output as untrusted input.

## Licensing of Contributions

PADOS Core is licensed under `AGPL-3.0-only`, and the copyright holder keeps the ability to offer it under a separate commercial license (see [LICENSING.md](LICENSING.md) and [COMMERCIAL.md](COMMERCIAL.md)). To keep that possible, contributions will require a signed Contributor License Agreement (CLA) once one is published. The CLA and its signing process are pending legal review; the reasoning is in [docs/legal/CLA_RECOMMENDATION.md](docs/legal/CLA_RECOMMENDATION.md).

**Until the CLA is published, external code contributions are not accepted.** Bug reports, security reports (see [SECURITY.md](SECURITY.md)) and discussion in issues are welcome. Do not send code you do not own or are not allowed to contribute (for example employer-owned code without permission).

New source files carry `SPDX-License-Identifier: AGPL-3.0-only` (`scripts/release/apply_spdx_headers.py`). A future `sdk/` area may be `Apache-2.0`; it will say so in its own `LICENSE`.

The PADOS name and logo are reserved; see the trademark section of [LICENSING.md](LICENSING.md#trademark).

## Before a Change

- Read [CONSTITUTION.md](CONSTITUTION.md), [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY.md](SECURITY.md), and [PLANS.md](PLANS.md).
- Work from an isolated non-main branch or worktree. Do not edit the protected canonical Windows checkout during validation.
- Keep changes within a bounded area and record any cross-platform effects.

## Implementation Expectations

- Preserve project, classification, ScopeVersion, authorization, provenance, and receipt bindings across protocol conversions.
- Reuse the existing scheduler, router, audit, Memory, and CAS authorities.
- Keep Work Graph and UI state as projections. Browser actions must use authenticated Remote Operations intents.
- Do not auto-install or authorize Skills, MCPs, tools, or models.
- Do not add provider access values to the browser bundle or commit local configuration.
- Prefer deterministic tests and explicit evidence over claims based on model output.

## Validation and Evidence

Run the relevant Python and frontend checks from their project directories. Record the exact command, exit code, summary, and log path in an acceptance report under `runtime/artifacts/reports/`. Do not claim a check passed unless its output is available.

For cross-platform behavior, validate the same code tree on Windows native and VMware Linux. Keep platform evidence separate from code changes where practical.

## Review and Release

Request independent review after implementation. Resolve critical and high findings before dogfood. Do not merge, push, publish, or release without explicit authorization. CI success does not authorize deployment. Public redistribution also requires resolved provenance and license clearance.
