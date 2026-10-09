# GitHub Pages deployment template (inactive)

`pages.workflow.yml.template` is the static Work Map UI build-and-deploy workflow. It is **inactive**: GitHub runs
only files under `.github/workflows/`, and this template is kept outside that directory on purpose.

Why it is not active: its deploy job needs the OIDC `id-token` write scope, the `pages` write scope and a
`github-pages` deployment environment. The Secure CI/CD policy (`src/pados/release/workflow_policy.py`,
`docs/supply_chain/SECURE_CICD_V1.md`) allows the `id-token` write scope only in the release attestation job and
forbids deploy steps and deployment environments in every workflow. CI SUCCESS != DEPLOYMENT AUTHORIZATION.

Activating it is a release decision, not a code change. It requires all of:

1. An explicit human decision to publish the static UI, recorded in the architecture decision log with its scope
   (which origin, which branch, who approves each deployment).
2. A reviewed, narrowly scoped policy exception in `workflow_policy.py` (for example a `WRITE_ALLOWLIST` entry for
   exactly `("pages.yml", "deploy")` with `id-token` and `pages`, and an environment allowance for that job only),
   with tests proving every other workflow is still refused.
3. Repository-side protection on the `github-pages` environment (required reviewers, `main` only) configured by the
   repository owner; workflow YAML cannot establish it.
4. A GitHub credential with the `workflow` scope to push a file under `.github/workflows/`.

Until then `tests/productization/test_pages_workflow_hygiene.py` asserts that the template stays outside
`.github/workflows/`, keeps its manual `publish` gate (default `false`) and that no active workflow deploys Pages.
