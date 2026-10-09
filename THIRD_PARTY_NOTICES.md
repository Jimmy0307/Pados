# Third-party notices

PADOS-owned source is licensed under `AGPL-3.0-only` (`LICENSE`, `NOTICE`, `LICENSING.md`). **Third-party
components retain their own licenses; nothing here relicenses them.** The compatibility notes below are an
engineering inventory, not legal advice.

PADOS vendors no third-party source code and modifies none. The full inventory is produced by
`scripts/release/dependency_distribution.py` (and `scripts/release/license_inventory.py`), run on the release branch.
Inventory basis for this page: `requirements.lock` and `web/package-lock.json` at the AGPL relicensing commit
(239 locked packages: 36 Python runtime, 4 npm bundled, the rest development/build only).

## AGPL-3.0 compatibility summary

| License family | Where | Compatible with distributing PADOS under AGPL-3.0-only? |
|---|---|---|
| MIT, BSD-2-Clause, BSD-3-Clause, ISC, PSF-2.0 | Most runtime and all bundled components | Yes (permissive; keep their notices) |
| Apache-2.0 (`tzdata`; `structlog` is `MIT OR Apache-2.0`) | Runtime | Yes, one way: Apache-2.0 code may be combined into a GPLv3/AGPLv3 work, not the reverse |
| LGPL-3.0-only (`psycopg`, `psycopg-binary`) | Runtime, unmodified, imported as a separately installed library | Yes (LGPLv3 is GPLv3-family; dynamic use of an unmodified library) |
| MPL-2.0 (`certifi`) | Runtime, unmodified data files | Yes (MPL-2.0 section 3.3 secondary-license compatibility; MPL file-level terms stay with certifi) |
| GPLv2-only, proprietary, SSPL, BUSL, "Commons Clause" | none found | Would be incompatible; none present |

Development and build tools (below) are not combined with or shipped in any PADOS artifact, so their licenses do
not affect PADOS's outbound license.

## Frontend bundle (`web/dist`): the only PADOS artifact that embeds third-party code

| Component | Version | License | AGPL-3.0 | Notice |
|---|---|---|---|---|
| react | 19.2.8 | MIT | compatible | `THIRD_PARTY_LICENSES/react-19.2.8.LICENSE.txt` |
| react-dom | 19.2.8 | MIT | compatible | `THIRD_PARTY_LICENSES/react-dom-19.2.8.LICENSE.txt` |
| scheduler | 0.27.0 | MIT | compatible | `THIRD_PARTY_LICENSES/scheduler-0.27.0.LICENSE.txt` |
| vite (module-preload helper) | 7.3.6 | MIT | compatible | `THIRD_PARTY_LICENSES/vite-7.3.6.LICENSE.txt` |

The minified bundle carries no license comments. Any distribution of `web/dist` must include these notices, and,
because the bundle is part of PADOS Core, the AGPL Corresponding Source (this repository's `web/` sources).

## Python runtime dependencies (installed by pip; not bundled in the `pados` wheel)

| Component | Version | License | AGPL-3.0 |
|---|---|---|---|
| alembic | 1.18.5 | MIT | compatible |
| annotated-doc | 0.0.4 | MIT | compatible |
| annotated-types | 0.7.0 | MIT | compatible |
| anyio | 4.14.1 | MIT | compatible |
| certifi | 2026.6.17 | MPL-2.0 | compatible (see below) |
| click | 8.4.2 | BSD-3-Clause | compatible |
| colorama (Windows only) | 0.4.6 | BSD-3-Clause | compatible |
| fastapi | 0.139.0 | MIT | compatible |
| greenlet | 3.5.3 | MIT AND PSF-2.0 | compatible |
| h11 | 0.16.0 | MIT | compatible |
| httpcore | 1.0.9 | BSD-3-Clause | compatible |
| httpx | 0.28.1 | BSD-3-Clause | compatible |
| idna | 3.18 | BSD-3-Clause | compatible |
| Mako | 1.3.12 | MIT | compatible |
| markdown-it-py | 4.2.0 | MIT | compatible |
| MarkupSafe | 3.0.3 | BSD-3-Clause | compatible |
| mdurl | 0.1.2 | MIT | compatible |
| psutil | 7.2.2 | BSD-3-Clause | compatible |
| psycopg | 3.3.4 | LGPL-3.0-only | compatible (see below) |
| psycopg-binary | 3.3.4 | LGPL-3.0-only | compatible (see below) |
| pydantic | 2.13.4 | MIT | compatible |
| pydantic-settings | 2.14.2 | MIT | compatible |
| pydantic_core | 2.46.4 | MIT | compatible |
| Pygments | 2.20.0 | BSD-2-Clause | compatible |
| pypdf | 6.14.2 | BSD-3-Clause | compatible |
| python-dotenv | 1.2.2 | BSD-3-Clause | compatible |
| rich | 15.0.0 | MIT | compatible |
| shellingham | 1.5.4 | ISC | compatible |
| SQLAlchemy | 2.0.51 | MIT | compatible |
| starlette | 1.3.1 | BSD-3-Clause | compatible |
| structlog | 25.5.0 | MIT OR Apache-2.0 | compatible |
| typer | 0.26.8 | MIT | compatible |
| typing-inspection | 0.4.2 | MIT | compatible |
| typing_extensions | 4.16.0 | PSF-2.0 | compatible |
| tzdata (Windows only) | 2026.2 | Apache-2.0 | compatible (one way) |
| uvicorn | 0.51.0 | BSD-3-Clause | compatible |

Notes:

- **psycopg, psycopg-binary (LGPL-3.0-only).** Unmodified library, imported at runtime, installed from PyPI. The
  `psycopg-binary` wheel itself embeds native libraries: libpq (PostgreSQL License), OpenSSL libcrypto/libssl
  (Apache-2.0), MIT Kerberos, OpenLDAP, Cyrus SASL, PCRE, libselinux, and e2fsprogs libcom_err. Those obligations
  fall on whoever redistributes that wheel; PADOS does not. ⚖️ Legal review is required before shipping any image
  or bundle that contains it.
- **certifi (MPL-2.0).** Unmodified CA bundle, installed from PyPI. ⚖️ If redistributed, MPL source availability
  applies to the certifi files only.

## Optional engines and models (never installed or bundled by PADOS)

Operators may connect optional engines declared in capability packs (for example Tesseract, PaddleOCR, a
MinerU-style parser, Ultralytics-based training backends, local model runtimes) and model weights. They run as
separately installed programs or services and keep their own licenses. Some are themselves AGPL-3.0 (flagged in
the pack metadata as "license review required"). Model weights carry model-specific licenses that PADOS records
but does not grant.

## Development and build only (not shipped)

These include 190+ MIT and other permissive packages, plus:

- `pathspec` (MPL-2.0; lint and type tooling);
- `caniuse-lite` (CC-BY-4.0; browser data used at build time);
- `typescript` (Apache-2.0).

None of them is contained in any PADOS artifact.

## Copied, adapted and generated material

- No third-party source file is vendored, copied or modified; no tracked PADOS source carries a third-party
  copyright header.
- The eight files once adapted from earlier private projects were replaced by a clean-room reimplementation
  (`docs/product/THIRD_PARTY_PROVENANCE.md`, `docs/clean-room/`); the replacements are PADOS-owned and AGPL-licensed.
- Test fixtures under `tests/fixtures/ocr/` are synthetic and generated by `tests/fixtures/ocr/build_fixtures.py`.
- The icons in `web/public/` are PADOS project assets (see the trademark section of `LICENSING.md`).

## Container images

- PADOS distributes **no** container image.
- `docker-compose.yml` and the demo *reference* the upstream `pgvector/pgvector` image, which users pull themselves.
