# `main` branch protection plan

**Status: APPLIED, WITH DOCUMENTED DIFFERENCES.** Branch protection is a repository setting. The repository owner
applies it; no workflow can, and none does. The applied protection (recorded in
`docs/releases/PADOS_RC2_LOCAL_DOGFOOD_V1/DOGFOOD_BOUNDARY.md`, "Repository controls at the freeze") differs from the
plan below: 0 required approvals (solo maintainer), `dependency review` is a seventh required check, linear history
is not required, and conversation resolution is not required. The plan below is kept as written.

## Requirements

| Requirement | Setting |
| --- | --- |
| Changes reach `main` only through a pull request | `required_pull_request_reviews` enabled (1 approval; stale reviews dismissed) |
| Required PR CI checks must pass | `required_status_checks.strict = true` with the contexts below |
| No direct push to `main`, no force push, no deletion | `allow_force_pushes = false`, `allow_deletions = false`, `enforce_admins = true` |
| Linear, reviewable history | `required_linear_history = true`; conversations resolved |
| Ordinary PRs do **not** require RC packaging | RC CI (`rc-ci.yml`) contexts are deliberately **not** required: they run only on `rc/**` pushes or manual dispatch, never on pull requests |

**Required status-check contexts** (job names from `pr-ci` / `ci.yml` and `security.yml`):

- `linux (lint, types, tests, build)`
- `windows (native suites)`
- `macos (static and contract)`
- `codeql (python)`
- `codeql (javascript-typescript)`
- `secret scan and CI policy`

**Not required:**

- `dependency review`: it reports `DEPENDENCY_REVIEW=ENVIRONMENT_BLOCKER` while the repository's Dependency
  graph is disabled. Add it to the required list after the owner enables Dependency graph and the job reports
  `PASS`.
- `rc-ci` contexts: RC CI is a release-candidate gate, not a merge gate.

## How to apply (repository owner)

1. Enable Dependency graph: Settings > Code security > Dependency graph.
2. Apply the protection as a repository administrator:

```
gh api -X PUT repos/Jimmy0307/Pados/branches/main/protection --input main-protection.json
```

`main-protection.json`:

```json
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "linux (lint, types, tests, build)",
      "windows (native suites)",
      "macos (static and contract)",
      "codeql (python)",
      "codeql (javascript-typescript)",
      "secret scan and CI policy"
    ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
```

3. Verify with `gh api repos/Jimmy0307/Pados/branches/main/protection`. The response must list the six contexts
   and `"allow_force_pushes": {"enabled": false}`.
