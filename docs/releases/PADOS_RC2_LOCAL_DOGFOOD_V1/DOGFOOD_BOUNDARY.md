# PADOS_RC2_LOCAL_DOGFOOD_V1: dogfood boundary

**Verdict: CONDITIONAL GO for local, supervised, non-sensitive dogfood. NO-GO for any public, network or production
use.**

The open P1 defects in `KNOWN_ISSUES.md` are contained only while every rule below holds. Breaking any rule removes
that containment.

## Allowed

- One operator on one machine, with manual supervision of every task.
- Personal non-sensitive projects, the PADOS project itself, public documentation, and local files that are safe to
  expose.
- Local inference on Ollama with `qwen3:8b`, under the accepted measured profile only.
- Fresh installs from a verified RC artifact. Upgrades only through `pados update`.

## Not allowed

- Company production data, HR data, 8D production data, or secrets as content.
- Binding the API to anything other than loopback, or any LAN or WAN exposure.
- Multi-user, shared-host or public deployment.
- Cloud workers (Codex, Claude) or the product loop. Never configure a proof/product database.
- Unattended or long-running agents.
- Pausing or resuming loops that have finished.
- Starting PADOS through the developer Python `pados` command, which accepts the default control-token hash.
- Running `pados init` against an existing database or runtime.
- Any production or customer use.

## Repository controls at the freeze

`main` branch protection:
- a pull request is required, with 0 approvals (solo maintainer);
- 7 strict required checks: `linux (lint, types, tests, build)`, `windows (native suites)`,
  `macos (static and contract)`, `codeql (python)`, `codeql (javascript-typescript)`, `secret scan and CI policy`,
  and `dependency review`;
- administrators are included;
- force pushes and branch deletion are blocked;
- conversation resolution is not required, by decision: the 26 open threads stay open as a register.

RC CI and packaging are not required checks. CI success never authorizes deployment or publication.

## Not released

- No npm package was published.
- No public release was made.
- Promotion beyond local dogfood requires a verified RC artifact, a passing live acceptance of the same commit and
  tree, closure or explicit acceptance of the P1 defects, and a human decision.
