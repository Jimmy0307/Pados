# Public release exclude manifest

**Machine-readable source:** `scripts/release/public_export_manifest.json` (`publication_enabled: false`).

**Checker:** `python scripts/release/public_export_check.py OUT.json`. It computes the export set from `git ls-files` and scans it for:

- local paths, private IPv4 addresses, emails and secret-shaped content;
- organisation, personal and local-model markers, loaded from an untracked private marker file so the markers themselves are never published.

Nothing is deleted from the internal branch; excluded paths remain private development evidence.

## Excluded from export

| Path | Reason |
|---|---|
| `runtime/**` (except `.gitkeep`), `work/**`, `outputs/**`, `wiki/**` | Generated evidence, logs, local run state, internal wiki |
| `.migration/**`, `docs/migration/**`, `docs/enterprise/DONOR_CAPABILITY_EXTRACTION_MATRIX.md` | Private donor inventories |
| `docs/p6_13/**`, `docs/architecture/vnext/baseline/**`, `docs/superpowers/**`, `docs/P6_15_*` | Milestone records with machine-specific data |
| Root milestone reports (`P6_*`, `P0_…`, `P7_…`, `FINAL_ACCEPTANCE_REPORT.md`, `PREFLIGHT_AUDIT.md`, `RELEASE_MANIFEST.json`, `PADOS_WEB_VALIDATION_REPORT.md`) | Internal reports |
| `CODEX_MASTER_PROMPT_…`, `PERSONAL_AI_WORKPLACE_START_HERE.md`, `AGENTS.md` | Agent prompts and personal notes (`release/public/CONTRIBUTING.md` replaces them) |
| Milestone scripts and launchers (`scripts/p6_15_*`, `scripts/run_p6_10_demos.py`, `Start-PADOS-P6-15-Corpus.ps1`, `Start/Stop-PADOS-Operator.ps1`, `config/release_manifest.json`) | Local paths |
| `docs/clean-room/SIMILARITY_REVIEW.json` | Internal audit record |
| `docs/private/**` | Organisation-specific drafts and validation notes (private by policy) |

The GitHub Pages deployment workflow is not excluded; it is shipped as the inactive template
`deploy/github-pages/pages.workflow.yml.template` (outside `.github/workflows/`, see its README).

## Export-time mapping

- `release/public/README.md`, `SECURITY.md` and `CONTRIBUTING.md` replace the internal root files of the same names. `CODE_OF_CONDUCT.md` and `MATURITY.md` are added.
- `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md` and `THIRD_PARTY_LICENSES/` ship as they are.

## Status of the previous blockers

- `PROVENANCE_DECISION_REQUIRED` is resolved by the clean-room reimplementation.
- `WORDING_CLEANUP_AT_EXPORT` is resolved: internal names were removed from the docstrings and docs, and the runbook uses `<PADOS_ROOT>`.
