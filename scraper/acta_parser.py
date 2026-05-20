"""Parse OCR'd FEB acta text into structured fields.

The acta is a standard federation form (referees, table officials, coaches,
captain, player roster). OCR output is noisy so this module uses tolerant,
section-anchored regex parsing. All sections fail soft — a bad section
appends a warning rather than aborting.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("basketaraba")


# Section anchors. Use \b only around ascii letters; Spanish accent variants
# are written explicitly because OCR sometimes drops them.
_ANCHOR_REFEREE_PRINCIPAL = re.compile(r"[ÁA]RBITRO\s+PRINCIPAL", re.IGNORECASE)
_ANCHOR_REFEREE_AUX = re.compile(
    r"[ÁA]RBITRO\s+(?:AUXILIAR|AYUDANTE|2[ºo°])", re.IGNORECASE,
)
_ANCHOR_MESA = re.compile(r"\bMESA\b", re.IGNORECASE)
_ANCHOR_ANOTADOR = re.compile(r"\bANOTADOR\b(?!\s*ES)", re.IGNORECASE)
_ANCHOR_AYUDANTE_ANOTADOR = re.compile(r"AYUDANTE\s+ANOTADOR", re.IGNORECASE)
_ANCHOR_CRONO = re.compile(r"CRONOMETRADOR", re.IGNORECASE)
_ANCHOR_OP24 = re.compile(
    r"OPERADOR\s+(?:DE\s+)?(?:24|VEINTICUATRO)\s*(?:SEGUNDOS)?",
    re.IGNORECASE,
)
_ANCHOR_COMISARIO = re.compile(r"COMISARIO(?:\s+T[EÉ]CNICO)?", re.IGNORECASE)
_ANCHOR_ENTRENADOR = re.compile(r"\bENTRENADOR\b", re.IGNORECASE)
_ANCHOR_AYUDANTE_ENTRENADOR = re.compile(r"AYUDANTE\s+ENTRENADOR", re.IGNORECASE)
_ANCHOR_VISITANTE = re.compile(r"VISITANTE|EQUIPO\s+B\b", re.IGNORECASE)
_ANCHOR_INCIDENCIAS = re.compile(r"INCIDENCIAS", re.IGNORECASE)
_ANCHOR_PABELLON = re.compile(r"PABELL[OÓ]N", re.IGNORECASE)
_ANCHOR_ASISTENCIA = re.compile(r"ASISTENCIA|ESPECTADORES|P[UÚ]BLICO", re.IGNORECASE)
_ANCHOR_FECHA = re.compile(r"FECHA", re.IGNORECASE)

# A "name" token in Spanish actas: uppercase letters incl. accented + spaces,
# 2+ chars, at least one space (forename + surname). Lowercase tail allowed
# because OCR occasionally returns mixed case.
_NAME_RE = re.compile(
    r"([A-ZÁÉÍÓÚÑÜ][A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-ZÁÉÍÓÚÑÜ][A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){1,5})"
)
# License: 4+ digits, sometimes prefixed with letters (e.g. "ARB1234"). The
# whole token is what callers care about.
_LICENSE_RE = re.compile(r"\b([A-Z]{0,4}\d{4,8})\b")

# Dorsal + name + license + entries glyph row. OCR may run cells together
# with variable whitespace. We're permissive: dorsal at start, the
# entry-state marker is the first single-character glyph after the name/license.
_DORSAL_LINE_RE = re.compile(
    r"^\s*(?P<dorsal>\d{1,2})\s+"
    r"(?P<rest>.+?)\s*$",
)

# Entry-state glyphs:
#   starters: ⊗ (and common OCR confusions ® @ O with no neighbors)
#   subs that played: X x
#   did not play: -
_STARTER_GLYPHS = {"⊗", "®", "@", "O"}
_SUB_GLYPHS = {"X", "x"}
_DNP_GLYPHS = {"-", "—", "–"}

_DATE_RE = re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})")
_TIME_RE = re.compile(r"\b(\d{1,2})[:.](\d{2})\b")
_NUMBER_RE = re.compile(r"\b(\d{1,6})\b")
# Attendance: 2-6 digit number not part of an HH:MM (or H:MM) time token.
_ATTENDANCE_NUMBER_RE = re.compile(r"\b(\d{2,6})\b(?!:)")


def parse_acta_text(text: str) -> dict[str, Any]:
    """Parse OCR'd FEB acta text into structured fields.

    Returns a dict with stable keys; missing sections become None / empty
    lists and a warning is appended to `result["warnings"]`.
    """
    warnings: list[str] = []
    result: dict[str, Any] = {
        "officials": {
            "referee_principal": _empty_person(),
            "referee_auxiliar": _empty_person(),
            "anotador": _empty_person(),
            "ayudante_anotador": _empty_person(),
            "cronometrador": _empty_person(),
            "operador_24": _empty_person(),
            "comisario": _empty_person(),
        },
        "home": _empty_team(),
        "away": _empty_team(),
        "venue": None,
        "date": None,
        "attendance": None,
        "notes": None,
        "warnings": warnings,
    }

    if not text or not text.strip():
        warnings.append("empty_text")
        return result

    # Normalize whitespace per line; keep line structure.
    lines = [re.sub(r"[\t ]+", " ", ln).strip() for ln in text.splitlines()]
    flat = "\n".join(lines)

    _safe(lambda: _parse_header(flat, result, warnings), warnings, "header_parse_failed")
    _safe(lambda: _parse_officials(flat, result, warnings), warnings, "officials_parse_failed")
    _safe(lambda: _parse_teams(flat, result, warnings), warnings, "teams_parse_failed")
    _safe(lambda: _parse_incidencias(flat, result), warnings, "incidencias_parse_failed")

    return result


# --- private helpers -------------------------------------------------------

def _empty_person() -> dict[str, Any]:
    return {"name": None, "license": None}


def _empty_team() -> dict[str, Any]:
    return {
        "coach": _empty_person(),
        "assistant_coach": _empty_person(),
        "captain_dorsal": None,
        "entries": [],
    }


def _safe(fn, warnings: list[str], warning_label: str) -> None:
    try:
        fn()
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("acta parse: %s: %s", warning_label, exc)
        warnings.append(warning_label)


def _section_between(text: str, start_pat: re.Pattern, end_pats: list[re.Pattern]) -> str | None:
    """Return text between first match of start_pat and the next end_pats match."""
    m = start_pat.search(text)
    if not m:
        return None
    start = m.end()
    end = len(text)
    for ep in end_pats:
        em = ep.search(text, pos=start)
        if em and em.start() < end:
            end = em.start()
    return text[start:end]


def _extract_person(segment: str) -> dict[str, Any]:
    """Pull the first (name, license) pair out of a small text segment."""
    if not segment:
        return _empty_person()
    # Only look at the first ~200 chars so we don't slurp the next section.
    segment = segment[:200]
    name_match = _NAME_RE.search(segment)
    name = name_match.group(1).strip() if name_match else None
    if name:
        name = re.sub(r"\s{2,}", " ", name)
    lic_match = _LICENSE_RE.search(segment)
    license_ = lic_match.group(1) if lic_match else None
    return {"name": name, "license": license_}


def _parse_header(text: str, result: dict[str, Any], warnings: list[str]) -> None:
    # Venue: line after PABELLÓN
    venue_match = _ANCHOR_PABELLON.search(text)
    if venue_match:
        tail = text[venue_match.end():venue_match.end() + 200]
        # Name follows on same line or next line, before another keyword.
        # Trim at next anchor word.
        tail = re.split(r"FECHA|HORA|ASISTENCIA|[\r\n]{2,}", tail, maxsplit=1)[0]
        candidate = tail.strip(" :-\n\r")
        # Take the first non-empty line.
        for line in candidate.splitlines():
            line = line.strip(" :-")
            if line and not re.fullmatch(r"\d+", line):
                result["venue"] = line
                break

    # Date: prefer dd/mm/yyyy
    date_match = _DATE_RE.search(text)
    if date_match:
        day, month, year = date_match.groups()
        try:
            d = int(day)
            mo = int(month)
            y = int(year)
            if y < 100:
                y += 2000
            if 1 <= d <= 31 and 1 <= mo <= 12 and 1900 < y < 2100:
                result["date"] = f"{y:04d}-{mo:02d}-{d:02d}"
            else:
                result["date"] = date_match.group(0)
        except ValueError:
            result["date"] = date_match.group(0)

    # Attendance: number near ASISTENCIA / ESPECTADORES. We skip any digit run
    # that is immediately followed by ":" so a co-occurring "1:30" time token
    # on the same line doesn't get picked up as attendance="1".
    att_match = _ANCHOR_ASISTENCIA.search(text)
    if att_match:
        tail = text[att_match.end():att_match.end() + 80]
        num = _ATTENDANCE_NUMBER_RE.search(tail)
        if num:
            try:
                result["attendance"] = int(num.group(1))
            except ValueError:
                pass


def _parse_officials(text: str, result: dict[str, Any], warnings: list[str]) -> None:
    end_anchors = [
        _ANCHOR_REFEREE_AUX, _ANCHOR_MESA, _ANCHOR_ANOTADOR,
        _ANCHOR_COMISARIO, _ANCHOR_ENTRENADOR, _ANCHOR_INCIDENCIAS,
    ]

    # Referee principal
    seg = _section_between(text, _ANCHOR_REFEREE_PRINCIPAL, end_anchors)
    if seg:
        result["officials"]["referee_principal"] = _extract_person(seg)

    # Referee auxiliar
    seg = _section_between(text, _ANCHOR_REFEREE_AUX, [
        _ANCHOR_MESA, _ANCHOR_ANOTADOR, _ANCHOR_COMISARIO,
        _ANCHOR_ENTRENADOR, _ANCHOR_INCIDENCIAS,
    ])
    if seg:
        result["officials"]["referee_auxiliar"] = _extract_person(seg)

    if (result["officials"]["referee_principal"]["name"] is None
            and result["officials"]["referee_auxiliar"]["name"] is None):
        warnings.append("no_referees_found")

    # Table officials: anotador, ayudante anotador, cronometrador, operador 24
    seg = _section_between(text, _ANCHOR_AYUDANTE_ANOTADOR, [
        _ANCHOR_CRONO, _ANCHOR_OP24, _ANCHOR_COMISARIO,
        _ANCHOR_ENTRENADOR, _ANCHOR_INCIDENCIAS,
    ])
    if seg:
        result["officials"]["ayudante_anotador"] = _extract_person(seg)

    # Plain "ANOTADOR" — must not match "AYUDANTE ANOTADOR" first. We find the
    # first ANOTADOR occurrence that is NOT preceded by "AYUDANTE ".
    for m in _ANCHOR_ANOTADOR.finditer(text):
        prefix = text[max(0, m.start() - 10):m.start()].upper()
        if "AYUDANTE" in prefix:
            continue
        tail = text[m.end():m.end() + 200]
        result["officials"]["anotador"] = _extract_person(tail)
        break

    seg = _section_between(text, _ANCHOR_CRONO, [
        _ANCHOR_OP24, _ANCHOR_COMISARIO, _ANCHOR_ENTRENADOR, _ANCHOR_INCIDENCIAS,
    ])
    if seg:
        result["officials"]["cronometrador"] = _extract_person(seg)

    seg = _section_between(text, _ANCHOR_OP24, [
        _ANCHOR_COMISARIO, _ANCHOR_ENTRENADOR, _ANCHOR_INCIDENCIAS,
    ])
    if seg:
        result["officials"]["operador_24"] = _extract_person(seg)

    seg = _section_between(text, _ANCHOR_COMISARIO, [
        _ANCHOR_ENTRENADOR, _ANCHOR_INCIDENCIAS,
    ])
    if seg:
        result["officials"]["comisario"] = _extract_person(seg)


def _parse_teams(text: str, result: dict[str, Any], warnings: list[str]) -> None:
    """Split text in two halves around the visitante anchor, parse each."""
    visitante_match = _ANCHOR_VISITANTE.search(text)
    if visitante_match:
        home_text = text[:visitante_match.start()]
        away_text = text[visitante_match.end():]
    else:
        # Fall back to midpoint split.
        mid = len(text) // 2
        home_text = text[:mid]
        away_text = text[mid:]
        warnings.append("side_split_heuristic")

    _parse_one_team(home_text, result["home"], warnings, side="home")
    _parse_one_team(away_text, result["away"], warnings, side="away")


def _parse_one_team(segment: str, target: dict[str, Any],
                    warnings: list[str], *, side: str) -> None:
    # Coach (ENTRENADOR) — not AYUDANTE ENTRENADOR
    for m in _ANCHOR_ENTRENADOR.finditer(segment):
        prefix = segment[max(0, m.start() - 10):m.start()].upper()
        if "AYUDANTE" in prefix:
            continue
        tail = segment[m.end():m.end() + 200]
        target["coach"] = _extract_person(tail)
        break

    asst_match = _ANCHOR_AYUDANTE_ENTRENADOR.search(segment)
    if asst_match:
        tail = segment[asst_match.end():asst_match.end() + 200]
        target["assistant_coach"] = _extract_person(tail)

    entries, captain = _parse_entries(segment)
    target["entries"] = entries
    target["captain_dorsal"] = captain

    if entries and not any(e["status"] == "starter" for e in entries):
        warnings.append(f"no_starters_found_{side}")


def _parse_entries(segment: str) -> tuple[list[dict[str, Any]], int | None]:
    """Scan the player roster table. Returns (entries, captain_dorsal)."""
    entries: list[dict[str, Any]] = []
    captain: int | None = None
    seen_dorsals: set[int] = set()

    for raw_line in segment.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _DORSAL_LINE_RE.match(line)
        if not m:
            continue
        try:
            dorsal = int(m.group("dorsal"))
        except ValueError:
            continue
        if dorsal < 0 or dorsal > 99:
            continue
        if dorsal in seen_dorsals:
            continue

        rest = m.group("rest")
        # A valid roster line should contain a NAME (uppercase letters).
        if not _NAME_RE.search(rest):
            continue

        status = _classify_entry(rest)
        seen_dorsals.add(dorsal)
        entries.append({"dorsal": dorsal, "status": status})

        # Captain marker: ★ or " C " near the dorsal/name
        if captain is None and _has_captain_marker(line):
            captain = dorsal

    return entries, captain


def _classify_entry(rest: str) -> str:
    """Map the entries-column glyph to starter / sub / dnp.

    We scan single-character tokens (between whitespace) after the name and
    pick the first one that matches a known glyph.
    """
    tokens = re.findall(r"\S+", rest)
    for tok in tokens:
        if tok in _STARTER_GLYPHS:
            return "starter"
        if tok in _SUB_GLYPHS:
            return "sub"
        if tok in _DNP_GLYPHS:
            return "dnp"
    # Also check single chars embedded in a longer token (e.g. "⊗®" run-on)
    for ch in rest:
        if ch in _STARTER_GLYPHS:
            return "starter"
    for ch in rest:
        if ch in _SUB_GLYPHS:
            return "sub"
    return "dnp"


def _has_captain_marker(line: str) -> bool:
    if "★" in line or "✪" in line:
        return True
    # Standalone "C" used as captain flag. We're conservative: only inspect
    # the substring AFTER the license token on this row. If the row has no
    # license token, do not attempt captain detection (avoids matching a
    # header line that contains the column letter "C").
    lic = _LICENSE_RE.search(line)
    if not lic:
        return False
    tail = line[lic.end():]
    return bool(re.search(r"(?:^|\s)C(?:\s|$|[*★])", tail))


def _parse_incidencias(text: str, result: dict[str, Any]) -> None:
    m = _ANCHOR_INCIDENCIAS.search(text)
    if not m:
        return
    tail = text[m.end():m.end() + 2000].strip(" :\n\r-")
    if tail:
        # Trim trailing form-footer noise (signatures etc).
        tail = re.split(r"FIRMA[S]?\b|HORA\s+DE\s+CIERRE", tail, maxsplit=1, flags=re.IGNORECASE)[0]
        tail = tail.strip()
        if tail:
            result["notes"] = tail
