"""Run ocrmypdf on FEB acta PDFs to produce a searchable PDF + text sidecar.

The PDFs cached under `data/<season>/<group>/raw/acta_<id>.pdf` are image-only
scans. This module wraps the `ocrmypdf` CLI binary so downstream parsers
(see `scraper.acta_parser`) can work on plain text.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger("basketaraba")


def ocr_acta_pdf(
    pdf_path: Path,
    *,
    force: bool = False,
    language: str = "spa",
    timeout: float = 300.0,
) -> Path | None:
    """Run ocrmypdf on pdf_path. Returns path to the OCR'd PDF, or None on failure.

    Outputs (same dir as input):
      - <pdf_path stem>.ocr.pdf
      - <pdf_path stem>.ocr.txt
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        log.warning("ocr: source pdf missing: %s", pdf_path)
        return None

    out_pdf = pdf_path.with_suffix(".ocr.pdf")
    out_txt = pdf_path.with_suffix(".ocr.txt")

    if not force and _is_cached(pdf_path, out_pdf, out_txt):
        log.debug("ocr: cached output up to date for %s", pdf_path.name)
        return out_pdf

    tmp_pdf = out_pdf.with_suffix(out_pdf.suffix + ".tmp")
    tmp_txt = out_txt.with_suffix(out_txt.suffix + ".tmp")

    cmd = [
        "ocrmypdf",
        "--skip-text",
        "-l", language,
        "--sidecar", str(tmp_txt),
        str(pdf_path),
        str(tmp_pdf),
    ]

    log.info("ocr: starting %s (lang=%s)", pdf_path.name, language)
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        log.warning("ocr: ocrmypdf binary not found on PATH (skipping %s)", pdf_path.name)
        _cleanup(tmp_pdf, tmp_txt)
        return None
    except subprocess.TimeoutExpired:
        log.warning("ocr: timeout after %.1fs for %s", timeout, pdf_path.name)
        _cleanup(tmp_pdf, tmp_txt)
        return None
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("ocr: unexpected error for %s: %s", pdf_path.name, exc)
        _cleanup(tmp_pdf, tmp_txt)
        return None

    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        log.warning(
            "ocr: ocrmypdf exit %d for %s: %s",
            result.returncode, pdf_path.name, stderr[:300],
        )
        _cleanup(tmp_pdf, tmp_txt)
        return None

    if not tmp_pdf.exists() or not tmp_txt.exists():
        log.warning("ocr: ocrmypdf returned 0 but outputs missing for %s", pdf_path.name)
        _cleanup(tmp_pdf, tmp_txt)
        return None

    try:
        os.replace(tmp_pdf, out_pdf)
        os.replace(tmp_txt, out_txt)
    except OSError as exc:
        log.warning("ocr: failed to finalize outputs for %s: %s", pdf_path.name, exc)
        _cleanup(tmp_pdf, tmp_txt)
        return None

    duration = time.monotonic() - start
    log.info("ocr: done %s in %.1fs", pdf_path.name, duration)
    return out_pdf


def _is_cached(src: Path, out_pdf: Path, out_txt: Path) -> bool:
    if not (out_pdf.exists() and out_txt.exists()):
        return False
    try:
        src_mtime = src.stat().st_mtime
        return out_pdf.stat().st_mtime >= src_mtime and out_txt.stat().st_mtime >= src_mtime
    except OSError:
        return False


def _cleanup(*paths: Path) -> None:
    for p in paths:
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
