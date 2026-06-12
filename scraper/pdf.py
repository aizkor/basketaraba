"""Download the official FEB PDF acta for a finished match.

The endpoint is public and returns ~5MB image-based PDFs:

These PDFs are the source for OCR-based fact-checking in Phase 2 of the
improvements plan (see plans/improvements_plan.md, section E1).

The PDFs are written to the per-group raw cache (`data/<group>/raw/`) which is
gitignored — they are NOT committed.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import requests

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).parent.parent))
from config import ACTA_URL_TEMPLATE as PDF_URL_TEMPLATE

from .common import USER_AGENT

log = logging.getLogger("basketaraba")
_CHUNK_SIZE = 64 * 1024  # 64 KiB chunks for ~5 MiB downloads


def download_acta_pdf(
    partido_id: str,
    raw_dir: Path,
    *,
    force: bool = False,
    session: Optional[requests.Session] = None,
    sleep_seconds: float = 0.0,
    timeout: float = 30.0,
) -> Optional[Path]:
    """Download the FEB PDF acta for a match.

    Returns the path to the cached PDF on success, or None if the PDF could not
    be downloaded (404, wrong content-type, network error, etc.) — never
    raises for expected outcomes so the calling pipeline keeps going.

    Args:
        partido_id: Match id (the `partido_id` field in matches/<id>.json).
        raw_dir: Directory to write into (typically `data/<group>/raw/`).
        force: If True, re-download even if a cached file exists.
        session: Optional `requests.Session` to reuse the connection pool and
            HTTP keep-alive across calls. A transient session is created when
            None.
        sleep_seconds: Seconds to sleep AFTER a successful download (for
            rate-limiting). The caller can also sleep between matches.
        timeout: Per-request timeout in seconds.

    Returns:
        Path to the downloaded `.pdf` file on success, or None on any
        non-success outcome.
    """
    target = raw_dir / f"acta_{partido_id}.pdf"
    if target.exists() and not force:
        log.debug("acta pdf cached: %s", target.name)
        return target

    url = PDF_URL_TEMPLATE.format(partido_id=partido_id)
    own_session = session is None
    sess = session or requests.Session()
    if own_session:
        sess.headers["User-Agent"] = USER_AGENT
    else:
        # Make sure the UA is set even on caller-provided sessions.
        sess.headers.setdefault("User-Agent", USER_AGENT)

    try:
        resp = sess.get(url, timeout=timeout, stream=True, headers={"Accept": "application/pdf"})
    except requests.RequestException as exc:
        log.warning("acta pdf network error for %s: %s", partido_id, exc)
        if own_session:
            sess.close()
        return None

    try:
        if resp.status_code == 404:
            log.debug("acta pdf not published yet (404): %s", partido_id)
            return None
        if resp.status_code != 200:
            log.warning("acta pdf HTTP %d for %s", resp.status_code, partido_id)
            return None

        content_type = (resp.headers.get("Content-Type") or "").lower().strip()
        if not content_type.startswith("application/pdf"):
            log.warning(
                "acta pdf wrong Content-Type for %s: %r (expected application/pdf)",
                partido_id, content_type,
            )
            return None

        raw_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_suffix(target.suffix + ".tmp")
        try:
            with tmp_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                    if chunk:
                        fh.write(chunk)
            tmp_path.replace(target)
        except Exception:
            # Clean up any partial download before re-raising
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        return target
    finally:
        try:
            resp.close()
        except Exception:
            pass
        if own_session:
            sess.close()
