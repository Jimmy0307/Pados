# Licensing

> This page summarises the licensing of this repository in plain language. It is **not legal advice**. The
> license texts themselves (`LICENSE`, `THIRD_PARTY_LICENSES/`) are authoritative. Consult your own counsel for
> questions about your obligations.

## What is under which license

| Area | License | Where it is stated |
|---|---|---|
| **PADOS Core**: the server, CLI, workers, capability packs, release tooling, scripts, the web operator workspace (`web/`), configuration examples and documentation, i.e. every PADOS-owned file unless a directory states otherwise | `AGPL-3.0-only` | `LICENSE`, `NOTICE`, `pyproject.toml` (`license = "AGPL-3.0-only"`), `web/package.json`, `SPDX-License-Identifier` headers |
| **Future public SDKs, protocol schemas and integration contracts** | `Apache-2.0`, only once they live in their own directory (designated: `sdk/`) with their own `LICENSE` file and `SPDX-License-Identifier: Apache-2.0` headers | Not present today (see below) |
| **Third-party components** (Python and npm dependencies, the code they contribute to the web bundle) | Their own licenses, unchanged | `THIRD_PARTY_NOTICES.md`, `THIRD_PARTY_LICENSES/` |

### Separately licensed SDK area (policy; no such component exists yet)

Owner decision OD-2 permits `Apache-2.0` for public SDKs, protocol schemas and integration contracts **only** when
they are cleanly separated. An audit of the current tree found no component that meets that bar: protocol and
contract models (for example the compute-node manifest and receipt models) import PADOS Core modules and are part
of the AGPL-licensed Core. Nothing has been moved or relicensed.

When a separable component is created it must:

1. live under `sdk/` (for example `sdk/python/`, `sdk/schemas/`), never inside `src/pados/`;
2. carry its own `sdk/LICENSE` (Apache-2.0 text) and `SPDX-License-Identifier: Apache-2.0` headers;
3. not import PADOS Core code (Core may depend on the SDK, never the reverse);
4. be added to `THIRD_PARTY_NOTICES.md`/`LICENSING.md` and to the SPDX header tool's Apache area.

Code already published under AGPL-3.0 can be offered additionally under Apache-2.0 only by the copyright holders of
that code.

## Dual licensing

PADOS Core is distributed under `AGPL-3.0-only`. The copyright holder retains the ability to offer the same code
under separate commercial license terms to parties who cannot or do not want to accept the AGPL. Commercial terms
are never implied by this repository; see [COMMERCIAL.md](COMMERCIAL.md). Keeping that option available is the
reason contributions will require a contributor license agreement (see [CONTRIBUTING.md](CONTRIBUTING.md) and
[docs/legal/CLA_RECOMMENDATION.md](docs/legal/CLA_RECOMMENDATION.md)).

No BSL, source-available or "modified" license terms apply to PADOS Core.

## Trademark

"PADOS" and the PADOS logo (including `web/public/pados-icon.svg` and `web/public/favicon.svg`) are reserved by the
project. The AGPL and any other code license grant **no** trademark rights. You may say truthfully that your work is
based on PADOS, but a fork or modified version must not use the PADOS name or logo in a way that suggests it is the
official PADOS product or is endorsed by the PADOS project. Rename modified distributions.

## AGPL network use (section 13) in practice

Plain-language summary for PADOS deployments; the license text controls.

- **Running unmodified PADOS** for yourself or your organisation, including as a server reached over a network,
  does not by itself oblige you to publish anything. Keep the license and notices intact, and keep the source
  offer that unmodified PADOS already points to (this repository).
- **Modifying PADOS and letting users interact with it over a network** (the web workspace, the REST/remote
  operations API, a compute-node endpoint used by other people) triggers section 13: you must prominently offer
  those users the Corresponding Source of your modified version, under the AGPL, at no charge (for example a
  "Source" link in the UI pointing to your repository or archive).
- **Distributing PADOS** (wheels, archives, images, the built `web/dist` bundle) requires providing the
  Corresponding Source and keeping `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md` and `THIRD_PARTY_LICENSES/`.
- **Separate programs** that talk to PADOS only through its network APIs or files (an external model runtime, an
  MCP server, an enterprise system calling the API) are not made AGPL by that communication alone. Whether code
  that is linked into or imported by PADOS forms a single combined work is a legal question; ask counsel.
- **Model weights, datasets and project content** processed by PADOS keep their own licenses and ownership.
  PADOS does not relicense them, and they are not PADOS source code.

If these obligations do not fit your use, a commercial license may be available ([COMMERCIAL.md](COMMERCIAL.md)).
