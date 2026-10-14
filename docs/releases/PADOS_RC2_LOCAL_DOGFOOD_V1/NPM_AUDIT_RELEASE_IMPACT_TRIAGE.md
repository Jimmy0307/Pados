# PADOS_RC2_LOCAL_DOGFOOD_V1: npm audit release-impact triage

During the frontend `npm ci` step, PR CI run 38447098226 (Linux) and RC CI run 38447092231 (win32-x64 and linux-x64
packaging) both reported `4 vulnerabilities (1 moderate, 1 high, 2 critical)`. The audit was reproduced read-only
with `npm audit --package-lock-only` on `web/package.json` and `web/package-lock.json` at the tested commit `dd72831`.
`npm audit fix` was not run and no package was changed.

## Findings

| Package@version | Severity | Advisory | Path | Direct or transitive | Lockfile | In the shipped RC artifact | Reachable at runtime |
|---|---|---|---|---|---|---|---|
| vitest@3.2.7 | critical (aggregate via tinypool) and moderate | GHSA-82fw-gwwq-j7x9 | `web` devDependency → vitest | direct | dev | NO | NO (test runner; `npm test` only) |
| tinypool@1.1.1 | critical | GHSA-5gmw-xhrv-c9v3, GHSA-85c8-ppgw-ccpr | vitest → tinypool | transitive | dev | NO | NO |
| @vitest/mocker@3.2.7 | moderate | GHSA-82fw-gwwq-j7x9 | vitest → @vitest/mocker | transitive | dev | NO | NO |
| source-map-js@1.2.1 | high | GHSA-68fv-2mgg-jv7q (event-loop DoS on crafted source maps) | vite → postcss → source-map-js | transitive | dev | NO | NO (build time only; `vite build` in CI processes first-party sources, never untrusted source maps) |

## Evidence

- A production-only audit (`npm audit --package-lock-only --omit=dev`) reports **0** vulnerabilities. The frontend's
  only production dependencies are `react` and `react-dom`.
- All four packages are marked `dev: true` in `web/package-lock.json`.
- The four shipped tarballs (win32-x64 and linux-x64, runtime and CLI, version 0.1.0-alpha.1) were inspected. Their
  hashes match `SHA256SUMS`. Each of them contains:
  - no `node_modules` entries;
  - no source maps;
  - no occurrence of `vitest`, `tinypool`, `@vitest/mocker`, `source-map-js` or `postcss` in any shipped `.js`,
    `.mjs`, `.cjs` or `.json` file.
- Of the frontend, the runtime ships only the compiled `runtime/app/web-dist/` bundle. Packaging produces it with
  `npm ci --ignore-scripts && npm run build` and passes it to `build_runtime.py --web-dist web/dist`.
- The CLI package has no npm `dependencies`; its only optional dependencies are its own platform runtime packages.

## Verdict

`NPM_AUDIT_RELEASE_BLOCKER=NO`. Every critical and high finding is in development, build or test tooling only, and
none is present in the shipped runtime.

Follow-up, not a release blocker: move `vitest` past GHSA-82fw-gwwq-j7x9 and the tinypool advisories, and move
`source-map-js` to 1.2.2 or later, in a separate dependency change.
