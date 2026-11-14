# Scanned-PDF OCR: assessment, candidate and first live execution (DI-4)

Status: **candidate, not wired into a template.** One live execution has succeeded, on clean synthetic renders
(details below). Real-scan quality is NOT_VALIDATED, and Windows still fails closed (R19-3).

## What was missing

The OCR pack already had:

- the text layer (pypdf, isolated);
- `NEEDS_OCR` page detection;
- a Tesseract CLI adapter that accepts **images only** ("PDFs need rasterization first").

Nothing turned a PDF page into an image. Tesseract was not installed anywhere, and the VM has no internet and no
passwordless sudo.

## Options assessed

| Option | License | Size | Isolation fit | Verdict |
| --- | --- | --- | --- | --- |
| **pypdfium2 5.15.2** (PDFium) | BSD-3-Clause / Apache-2.0. Bundled: freetype (FTL), libjpeg-turbo, libpng, zlib, ICU, lcms2, OpenJPEG, libtiff, harfbuzz, abseil, agg; texts ship in the wheel | ~4 MB wheel, Windows and manylinux, no system packages | Renders single-threaded, so it runs under the pack's `RLIMIT_NPROC = 0` child | **Chosen** (smallest, self-contained) |
| poppler `pdftoppm` | GPL-2.0-or-later | system package | Separate binary under `run_bounded` | Rejected: GPL binary, needs apt/sudo |
| Ghostscript | AGPL-3.0 | large | binary | Rejected: size and license obligations |
| PyMuPDF | AGPL-3.0 / commercial | ~20 MB | in-process | Rejected: license and in-process |
| Tesseract 5.3.0 (Debian bookworm) | Apache-2.0; Leptonica BSD-2-style; tessdata Apache-2.0 | 7 packages, ~7.7 MB | Existing `TesseractCliAdapter` path, under `run_bounded` rlimits | **Chosen** (existing adapter) |
| PaddleOCR | Apache-2.0, but needs paddlepaddle (large) | ~1 GB | binding worker | Deferred: size; no measured need |
| Docling | MIT, but needs torch and models | multi-GB | — | Deferred: size |

## Candidate (PR #25)

- `pados.packs.ocr._raster_worker`: a new isolated child, launched like `_pdf_worker`:
  - the pack's rlimits are applied first, with no thread or process, an empty environment and bounded output;
  - the page size is checked **before** rendering (`max_pixels`);
  - grayscale PNG is encoded with the standard library (no Pillow);
  - errors are closed `RASTER_*` codes, registered in `STAGE_CODES`.
- `pados.packs.ocr.raster.render_page_png` and `ocr_pdf_pages`: rasterize each page, then call the **existing**
  `tesseract_argv` / `run_bounded` / `parse_tesseract_tsv`. The result is one `ocr.engine-result/v1`.
- It fails closed on win32 (`OCR_ISOLATION_UNAVAILABLE`, nothing started).
- Tests are in `tests/packs/ocr/test_ocr_raster_candidate.py`. The OCR pack's own boundary tests refused the
  first version (a dynamic import, and unregistered codes); both were fixed.

## Isolated OCR worker image (VMware VM, offline)

1. On the host, resolve the bookworm dependency closure of `tesseract-ocr`, `tesseract-ocr-eng` and
   `tesseract-ocr-osd` against the `python:3.12-bookworm` image's installed packages. That gives 7 `.deb` files.
2. Download them over HTTPS and verify each against the Debian `Packages` SHA-256 (`tess-debs.SHA256SUMS`). The
   index came over HTTPS; the `InRelease` GPG signature was **not** checked (residual DI-12).
3. On the VM, install them with `dpkg -i` in a **networkless** build container: root inside the container only,
   no host sudo. This gives image `pados-ocr-tesseract:bookworm-5.3.0`.
4. Install PADOS at the exact SHA (`git archive`, non-editable), the locked wheelhouse and pypdfium2, offline.
   This gives `pados-ocr-worker:<sha>`.
5. Run with `--network none --user 1000:1000 --read-only --tmpfs /tmp --cap-drop ALL --security-opt
   no-new-privileges --memory 2g --pids-limit 64` and the inputs mounted read-only. Inside, the PADOS rlimit runner
   applies as usual.

## First live execution (2026-11-14, `f699344`, VMware Ubuntu 24.04)

- Input: an image-only PDF (PDFium render at 300 dpi of synthetic NCR pages; text layer 0 characters), made by
  `scripts/validation/scanned_pdf_ocr_check.py make`.
- Output:
  - 2 pages, 36 lines, 4.07 s;
  - **character similarity 1.00, word recall 1.00**;
  - mean confidence 0.959.
- What this proves: the raster → Tesseract execution path works end to end under PADOS isolation.
- What it does not prove: real-scan accuracy. The input is a clean digital render, with no skew, noise, blur,
  stamps or handwriting.

## Remaining work before an OCR lane can PASS

1. Wire the candidate as a second recognition stage for `NEEDS_OCR` pages, using the pack's existing hybrid
   pattern, with engine records and the document reference check.
2. Track R scans (`docs/packs/document_intelligence/EVALUATION_PROTOCOL.md`): report the character error rate on
   real scans.
3. Decide the deployment (owner): either a worker image on Linux, or Windows Job Object containment (R19-3).
4. Optionally verify the Debian `InRelease` signature in the image build.
