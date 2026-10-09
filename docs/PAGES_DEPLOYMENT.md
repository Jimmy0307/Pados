# Static PADOS Work Map Hosting

GitHub Pages distributes the static Work Map UI. It is not a PADOS backend, authority, scheduler, database, secret store, or provider gateway.

## Build

The Pages workflow is currently an **inactive template**, `deploy/github-pages/pages.workflow.yml.template`, kept outside `.github/workflows/` because its deploy job needs OIDC, the `pages` write scope and a deployment environment, which the Secure CI/CD policy refuses outside the release attestation job. Activating it requires a recorded human decision and a reviewed policy exception (see `deploy/github-pages/README.md`). The frontend itself is built and tested by `ci.yml` on every push.

Once activated, the workflow checks the frontend and uploads `web/dist` as a static Pages artifact. Pushes to `main` build the artifact but do not deploy it. A manual workflow dispatch defaults `publish` to `false`; setting it to `true` enables the separate Pages deployment job. Deployment remains a release action and must be separately authorized. Configure required reviewers on the GitHub `github-pages` environment before enabling deployment; workflow YAML cannot establish that repository-side protection.

The default project-site base is `/Pados/`. For a custom-domain root build, define the repository variable `PADOS_CUSTOM_DOMAIN` as a DNS hostname. The workflow validates that value, builds with base `/`, and writes the domain into the static artifact's `CNAME` file. Do not commit machine-specific Home Base addresses.

## Connect to Home Base

The signed-in user configures an HTTPS Home Base URL in the UI and confirms the exact origin before sending the actor proof. For a cross-origin Pages deployment, add the exact Pages or custom-domain origin to `allowed_origins` in the Remote Operations configuration. This list is empty by default. Wildcard origins and insecure non-local origins are refused.

Authentication is still required for every Remote Operations request. The browser keeps the entered access proof only in memory. A compromised allowed Pages build can still read that proof, access actor-visible project data, and issue actions allowed to that actor. Approvals and rejections additionally require a fresh WebAuthn step-up with user verification, bound by the server to the exact candidate and ScopeVersion and consumed once; see [SECURITY.md](../SECURITY.md) for the remaining transaction-display limitation. Do not place provider keys, project context, or Home Base secrets in Pages variables or build artifacts.

## Offline Behavior

The service worker caches the app shell and same-origin static assets. It bypasses `/remote/v1` and `/api`, and does not cache project payloads. If Home Base cannot be reached, the UI says `OFFLINE · REMOTE UNREACHABLE`; it does not claim queued or completed execution.

## Release Boundary

The Pages artifact contains the Work Map entry and its static resources; the retained operator page is excluded from that artifact. It carries no provider API keys or project database. Public deployment and the first public repository push have not been authorized by this implementation workflow. Repository publication also depends on resolved redistribution rights.
