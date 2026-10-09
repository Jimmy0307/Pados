# Security

## Report a Vulnerability

Do not report an exploitable issue in a public discussion. Use GitHub's private vulnerability reporting for `Jimmy0307/Pados` when it is enabled, or contact the repository owner privately. Do not include live access values, private project data, or customer information in a report.

## Trust Boundaries

- **Browser:** holds sign-in values in memory for the session, sends bounded intent, and reads projections. It has no provider credentials, scheduler authority, database access, or semantic commit path.
- **Static host:** distributes UI files and is part of the trusted computing base for browser sign-in. A compromised allowed build can read the in-memory reusable actor proof, read data available to that actor, and issue intents. If the actor may approve, malicious JavaScript can submit an approval that Home Base cannot distinguish from the user's click. Home Base still applies project authorization, candidate/scope binding, audit, and the existing CAS path; those controls do not prove genuine user presence.
- **Home Base:** authenticates remote actors, checks project access, reconstructs context from PADOS-owned stores, and routes actions through existing policy, scheduler, receipts, audit, and CAS paths.
- **Workers and providers:** return untrusted output. They cannot grant themselves authorization or mark their output semantically accepted.
- **Work Graph and UI projections:** are rebuildable views, not writable authority.
- **GitHub Actions:** build and test on ephemeral hosted runners. They receive no production authority database, Home Base shell, provider access, or persistent Home Base runner.

## Remote and PWA Controls

- Remote clients submit intent rather than project context or conversation history.
- Remote requests use authentication, project authorization, idempotency, and scope/version checks.
- Approval binds to the presented candidate and current scope; acceptance uses the existing CAS path.
- Cross-origin frontend access is off by default. If needed, configure exact HTTPS origins in the Remote Operations configuration. Wildcards, insecure non-local origins, credentials in origins, and path-bearing origins are rejected. CORS does not replace actor authentication.
- The PWA stores no sign-in proof or project payload in browser storage. Its service worker caches only shell files and static assets; `/remote/v1` and `/api` are network-only.
- Before connecting, the user must explicitly confirm the exact Home Base origin that will receive the actor proof. This helps catch endpoint mistakes; it does not protect against a compromised allowed static origin.
- Every APPROVE or REJECT requires a fresh WebAuthn step-up with user verification. The server derives the binding (actor, server session, project, task, candidate and digest, action, classification, expected ScopeVersion) and issues a short-lived challenge; a successful assertion yields a single-use, 60-second proof that the decision path consumes atomically after recomputing the binding from current authority. A standing authenticated session, a button click, or any client-held value is not sufficient. Without a configured relying party (`presence`), approvals fail closed.
- Residual risks: (1) the WebAuthn challenge is derived from the binding digest, so the stored signature commits to the exact decision, but authenticators do not display the candidate. (2) Attestation is not verified, so enrolling a device descriptor must be confirmed out of band by the operator. A compromised allowed static UI could ask the user to verify while showing different text. It still cannot approve without the user's gesture, cannot reuse a proof, and cannot reach candidates that are not verified and pending. Use a dedicated, least-privilege remote actor and prefer a same-origin Home Base UI for approvals.
- Do not add provider calls, provider keys, private LAN addresses, or persistent task context to the static bundle.
- Capability and MCP recommendations never auto-install, register, authorize, or select an integration.

## Safe Development

- Keep local configuration and access values out of source control. Use `SecretRef` or server-side configuration for runtime secrets.
- Do not weaken policy, sandbox, authentication, or approval checks to satisfy tests.
- Do not install optional models or external engines merely to make deterministic contract tests pass.
- Run security checks on the intended isolated worktree. Never send PADOS content to unrelated GitHub repositories.

## License and Public Release

PADOS Core is licensed `AGPL-3.0-only` (see `LICENSING.md`). Public redistribution clearance remains pending component provenance and rights review. A license file or successful CI run is not release authorization.
