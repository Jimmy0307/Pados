# Contributing to PADOS

Thank you for helping. PADOS is **alpha** software, and its core principle is fixed: `MODEL OUTPUT != TRUTH`. Contributions must not add any path that changes project state without receipt verification, validation and ScopeVersion CAS.

## Licensing of contributions (CLA required; not yet open)

PADOS Core is licensed under `AGPL-3.0-only`, and the copyright holder keeps the ability to offer it under a separate commercial license (`LICENSING.md`, `COMMERCIAL.md`). A Developer Certificate of Origin sign-off alone would not preserve that option, so contributions will require a signed Contributor License Agreement (CLA) once one is published. The CLA and its signing process are pending legal review (`docs/legal/CLA_RECOMMENDATION.md`).

**Until the CLA is published, external code contributions are not accepted.** Issues, bug reports and security reports (`SECURITY.md`) are welcome.

The PADOS name and logo are reserved; the code license grants no trademark rights (`LICENSING.md`).

## What we cannot accept

- Code copied from sources you do not have the right to contribute: employer-owned code without permission, or code under incompatible licenses.
- Company, customer or personal data; real credentials; machine-specific paths.
- Changes that weaken tests, fail-closed behaviour, or the authority chain.

## Workflow

Before sending a change:

- Run `ruff check src tests`, `mypy src`, `pytest`, and the web checks (`npm run typecheck && npm test && npm run lint && npm run build`).
- Keep changes focused.
- Explain behaviour changes in the pull request.
- Update docs when a public contract changes (docs/product/PUBLIC_API_BOUNDARY.md).
