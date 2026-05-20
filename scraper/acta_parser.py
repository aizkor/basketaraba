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
# Basque/bilingual Araba federation forms use "Arb. nagusia/Arb. principal"
# and "Gertalaria / Entrenador" — both variants are handled below.
_ANCHOR_REFEREE_PRINCIPAL = re.compile(
    r"[ÁA]RBITRO\s+PRINCIPAL|Arb\.\s*nagusia\s*/\s*Arb\.\s*principal",
    re.IGNORECASE,
)
_ANCHOR_REFEREE_AUX = re.compile(
    r"[ÁA]RBITRO\s+(?:AUXILIAR|AYUDANTE|2[ºo°])"
    r"|Arb\.\s*laguntzailea\s*/\s*Arb\.\s*auxiliar",
    re.IGNORECASE,
)
_ANCHOR_MESA = re.compile(r"\bMESA\b", re.IGNORECASE)
_ANCHOR_ANOTADOR = re.compile(
    r"\bANOTADOR\b(?!\s*ES)|Apuntatzailea\s*/\s*Anotador",
    re.IGNORECASE,
)
_ANCHOR_AYUDANTE_ANOTADOR = re.compile(
    r"AYUDANTE\s+ANOTADOR|Apuntatzaile\s*laguntzailea",
    re.IGNORECASE,
)
_ANCHOR_CRONO = re.compile(
    r"CRONOMETRADOR|Kronometratzailea\s*/\s*Cronometrador",
    re.IGNORECASE,
)
_ANCHOR_OP24 = re.compile(
    r"OPERADOR\s+(?:DE\s+)?(?:24|VEINTICUATRO)\s*(?:SEGUNDOS)?"
    r"|24\s*[\"']\s*Laguntzailea\s*/\s*Operador\s*24",
    re.IGNORECASE,
)
_ANCHOR_COMISARIO = re.compile(r"COMISARIO(?:\s+T[EÉ]CNICO)?", re.IGNORECASE)
_ANCHOR_ENTRENADOR = re.compile(
    r"\bENTRENADOR\b|Gertalaria\s*/\s*Entrenador",
    re.IGNORECASE,
)
_ANCHOR_AYUDANTE_ENTRENADOR = re.compile(
    r"AYUDANTE\s+ENTRENADOR|Entrenador\s+ayudante",
    re.IGNORECASE,
)
# Must match the section header line (with colon), not the form template header box.
# Araba forms: "Equipo B Taldea: TEAM NAME" marks start of away team section.
_ANCHOR_VISITANTE = re.compile(
    r"VISITANTE\b"
    r"|EQUIPO\s+B\s*:"           # standard FEB: "EQUIPO B:"
    r"|Equipo\s+B\s+Taldea\s*:", # Araba: "Equipo B Taldea: TEAM NAME"
    re.IGNORECASE,
)
_ANCHOR_INCIDENCIAS = re.compile(r"INCIDENCIAS", re.IGNORECASE)
_ANCHOR_PABELLON = re.compile(r"PABELL[OÓ]N", re.IGNORECASE)
_ANCHOR_ASISTENCIA = re.compile(r"ASISTENCIA|ESPECTADORES|P[UÚ]BLICO", re.IGNORECASE)
_ANCHOR_FECHA = re.compile(r"FECHA", re.IGNORECASE)

# Digital PDF header: "CATEGORY DD/MM/YYYY HH:MM NAME (LICENSE)"
# Seen in senior-masculine digital actas (no labeled referee anchors).
_DIGITAL_HEADER_RE = re.compile(
    r"(?:SENIOR|JUNIOR|C[AÁ]DETE|INFANTIL|MINI|PREBENJAM[IÍ]N)\s+[^\n]+?"
    r"\s+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s+(\d{1,2}:\d{2})\s+(.+)",
    re.IGNORECASE,
)
# Standalone secondary referee line: "SURNAME, INITIAL (LICENSE_NUM)"
# Matches a line that is nothing but a name + parenthesised number.
_DIGITAL_REF_LINE_RE = re.compile(
    r"^([A-ZÁÉÍÓÚÑÜ][A-Za-záéíóúñüÁÉÍÓÚÑÜ]+(?:\s+[A-Za-záéíóúñüÁÉÍÓÚÑÜ]+)*"
    r"(?:\s*,\s*[A-Za-záéíóúñüÁÉÍÓÚÑÜ.]+)?)"  # NAME, INITIAL
    r"\s*\((\d{2,5})\)\s*$",                    # (LICENSE_NUM)
    re.MULTILINE,
)
# Digital PDF coach line: NAME (LICENSE) optionally followed by stray numbers/symbols.
# Must NOT start with a 3+ digit player license or a category keyword.
_DIGITAL_COACH_RE = re.compile(
    r"^(?!\d{3,}\s)"                            # not a player-license line
    r"(?!(?:SENIOR|JUNIOR|C[AÁ]DETE|INFANTIL|MINI|PREBENJAM[IÍ]N)\b)"
    r"([A-ZÁÉÍÓÚÑÜ][A-Za-záéíóúñüÁÉÍÓÚÑÜ,.\s]+?)"
    r"\s*\((\d{2,6})\)",
    re.MULTILINE | re.IGNORECASE,
)
# Category keyword prefix — used to exclude the header line from coach detection.
_DIGITAL_CATEGORY_RE = re.compile(
    r"^(?:SENIOR|JUNIOR|C[AÁ]DETE|INFANTIL|MINI|PREBENJAM[IÍ]N)\b",
    re.IGNORECASE,
)

# A "name" token in Spanish actas: uppercase letters incl. accented + spaces,
# 2+ chars, at least one space (forename + surname). Lowercase tail allowed
# because OCR occasionally returns mixed case.
# Also handles "SURNAME, INITIAL" format used in Araba federation forms.
# OCR may insert a space before the comma (e.g. "SARCIA , A"), so we allow \s*,.
_NAME_RE = re.compile(
    r"([A-ZÁÉÍÓÚÑÜ][A-Za-zÁÉÍÓÚÑÜáéíóúñü]+"
    r"(?:"
    r"(?:\s+[A-ZÁÉÍÓÚÑÜ][A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){1,5}"  # normal: FIRSTNAME SURNAME
    r"|\s*,\s*[A-ZÁÉÍÓÚÑÜ][A-Za-zÁÉÍÓÚÑÜáéíóúñü.]*"     # Araba: SURNAME ,INITIAL
    r"))"
)
# License: 4+ digits, sometimes prefixed with letters (e.g. "ARB1234").
# Araba federation forms use slash-delimited numbers like "06/8" or "1/31".
_LICENSE_RE = re.compile(r"\b([A-Z]{0,4}\d{1,8}(?:/\d{1,4})?)\b")

# Roster line patterns — three supported formats:
#
# Standard FEB form:    DORSAL NAME [LICENSE] entries...
#   e.g. "4 GARCIA LOPEZ 12345 x 2 1 0 ..."
#
# Araba federation form (scanned): LICENSE |NAME DORSAL| entries...
#   e.g. "612 |MOYA, YERAY 3| x [s|8|3 | 25 ..."
#   e.g. "788 |FERNANDEZ DE LUCO, G (CAP) | 4 | (%) ..."
#
# Araba digital PDF form: LICENSE NAME DORSAL STATUS entries...
#   e.g. "635 SANTOS, I 1 X"
#   e.g. "585 CALLEJA, O (CAP) 8 X 8 3 3 10"
#
# We try the Araba scanned pattern first, then digital, then standard FEB.
# Named group "dorsal" always contains the player dorsal (1-2 digits).
# Named group "rest" contains name + entries for further parsing.
_DORSAL_LINE_RE = re.compile(
    r"^\s*(?P<dorsal>\d{1,2})\s+"
    r"(?P<rest>.+?)\s*$",
)
# Araba scanned form: LICENSE |NAME DORSAL_COL| entries...
# The leading number is 3+ digits (FEB license), name follows "|" (or OCR "l"),
# dorsal is 1-2 digits after the name (possibly after OCR noise).
# We allow the name to contain letters, commas, spaces, parentheses, dots (for
# "(CAP)", initials, etc.) but not raw digits by themselves.
_ARABA_ROSTER_RE = re.compile(
    r"^\s*\d{3,}\s+[|l\[]\s*"              # license (3+ digits) then "|","l","[" (OCR variants)
    r"(?P<name>[A-ZÁÉÍÓÚÑÜa-záéíóúñü]"    # name starts uppercase (or OCR lowercase)
    r"[A-ZÁÉÍÓÚÑÜa-záéíóúñü,.\s()★✪-]+?)"  # rest of name incl. (CAP), accented chars
    r"[^A-Za-zÁÉÍÓÚÑÜáéíóúñü0-9]*?"       # optional gap/noise (non-alphanum)
    r"(?P<dorsal>\d{1,2})"                 # dorsal: 1-2 digits
    r"(?P<rest>.*?)\s*$",                  # entries
)
# Araba digital PDF form: LICENSE NAME DORSAL STATUS...
# The leading number is 3+ digits (player license), name follows (no pipe),
# dorsal is 1-2 digits after name, then status glyph (X, -, or parenthesised).
_ARABA_DIGITAL_ROSTER_RE = re.compile(
    r"^\s*\d{3,}\s+"                        # license (3+ digits)
    r"(?P<name>[A-ZÁÉÍÓÚÑÜ]"               # name starts uppercase
    r"[A-Za-záéíóúñüÁÉÍÓÚÑÜ,.\s()★✪-]*?)"  # rest of name incl. (CAP)
    r"\s+(?P<dorsal>\d{1,2})\s+"           # SPACE dorsal SPACE
    r"(?P<rest>.+)$",                       # status + stats
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

    # Referee auxiliar — clamp to 200 chars and stop at a blank line to avoid
    # spilling into team-section content (Araba forms often leave this blank).
    m_aux = _ANCHOR_REFEREE_AUX.search(text)
    if m_aux:
        chunk = text[m_aux.end():m_aux.end() + 200]
        blank_line = re.search(r"\n\s*\n", chunk)
        if blank_line:
            chunk = chunk[:blank_line.start()]
        result["officials"]["referee_auxiliar"] = _extract_person(chunk)

    if (result["officials"]["referee_principal"]["name"] is None
            and result["officials"]["referee_auxiliar"]["name"] is None):
        # Fallback: digital PDF format — no labeled anchors.
        # Primary referee is embedded in the category/date header line;
        # secondary referees appear as standalone "NAME (LICENSE)" lines
        # in the last 30 % of the document.
        hdr_match = _DIGITAL_HEADER_RE.search(text)
        if hdr_match:
            primary_segment = hdr_match.group(3)
            result["officials"]["referee_principal"] = _extract_person(primary_segment)

            # Secondary refs: scan the last 30 % of text for standalone lines.
            tail_start = int(len(text) * 0.70)
            tail = text[tail_start:]
            secondary_found = False
            for ref_m in _DIGITAL_REF_LINE_RE.finditer(tail):
                name_part = ref_m.group(1).strip()
                lic_part = ref_m.group(2)
                # Skip if this line is identical to the primary referee name.
                primary_name = result["officials"]["referee_principal"]["name"] or ""
                if name_part.upper() == primary_name.upper():
                    continue
                result["officials"]["referee_auxiliar"] = {
                    "name": name_part,
                    "license": lic_part,
                }
                secondary_found = True
                break  # take the first non-primary match

            # Flag which path was used; only warn "no_referees_found" if
            # the digital fallback also came up empty.
            if result["officials"]["referee_principal"]["name"] is not None:
                warnings.append("digital_pdf_format")
            else:
                warnings.append("no_referees_found")
        else:
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
        result["officials"]["cronometrador"] = _extract_person(seg[:200])

    seg = _section_between(text, _ANCHOR_OP24, [
        _ANCHOR_COMISARIO, _ANCHOR_ENTRENADOR, _ANCHOR_INCIDENCIAS,
    ])
    if seg:
        # Clamp and stop at footer noise (e.g. "Arabako Foru" line).
        seg = seg[:200]
        blank = re.search(r"\n\s*\n", seg)
        if blank:
            seg = seg[:blank.start()]
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
        _parse_one_team(home_text, result["home"], warnings, side="home")
        _parse_one_team(away_text, result["away"], warnings, side="away")
        return

    # Digital PDF format: no labeled VISITANTE anchor.
    # Team names appear on lines 1 and 2 (line 0 is "X").
    # The away block starts at the 2nd standalone occurrence of the away team name.
    if "digital_pdf_format" in warnings:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) >= 3:
            away_name = lines[2]
            away_pat = re.compile(
                r"(?m)^" + re.escape(away_name) + r"\s*$"
            )
            away_matches = list(away_pat.finditer(text))
            if len(away_matches) >= 2:
                split_pos = away_matches[1].start()
                home_text = text[:split_pos]
                away_text = text[split_pos:]
                _parse_one_team(home_text, result["home"], warnings, side="home")
                _parse_one_team(away_text, result["away"], warnings, side="away")
                _parse_digital_coaches(home_text, away_text, result, warnings)
                return

    # Fall back to midpoint split.
    mid = len(text) // 2
    home_text = text[:mid]
    away_text = text[mid:]
    warnings.append("side_split_heuristic")
    _parse_one_team(home_text, result["home"], warnings, side="home")
    _parse_one_team(away_text, result["away"], warnings, side="away")


def _parse_digital_coaches(
    home_text: str, away_text: str, result: dict[str, Any], warnings: list[str]
) -> None:
    """Extract coaches from digital PDF blocks where ENTRENADOR label is absent.

    Home block: NAME (LICENSE) lines that are not player-license lines and not
    the category/header line → home coaches in order.
    Away block: same set, but the very last bare NAME (LICENSE) line (no trailing
    content) is the secondary referee already captured; preceding ones = away coaches.
    """
    def _collect_coach_lines(block: str) -> list[tuple[str, str]]:
        """Return [(name, license), ...] for coach-candidate lines in block."""
        results = []
        for line in block.splitlines():
            ls = line.strip()
            if not ls:
                continue
            # Must contain a parenthesised license number
            if not re.search(r"\(\d{2,6}\)", ls):
                continue
            # Skip player lines (start with 3+ digit license)
            if re.match(r"^\d{3,}\s", ls):
                continue
            # Skip the category/date/referee header line
            if _DIGITAL_CATEGORY_RE.match(ls):
                continue
            # Skip "Powered by TCPDF" and similar footers
            if re.match(r"Powered\b", ls, re.IGNORECASE):
                continue
            m = _DIGITAL_COACH_RE.match(ls)
            if m:
                name = m.group(1).strip().rstrip(",").strip()
                name = re.sub(r"\s{2,}", " ", name)
                lic = m.group(2)
                results.append((name, lic))
        return results

    # Home coaches
    home_coaches = _collect_coach_lines(home_text)
    if home_coaches:
        result["home"]["coach"] = {"name": home_coaches[0][0], "license": home_coaches[0][1]}
    if len(home_coaches) >= 2:
        result["home"]["assistant_coach"] = {"name": home_coaches[1][0], "license": home_coaches[1][1]}

    # Away coaches — exclude the last bare NAME (LICENSE) line (= secondary referee)
    away_candidates = _collect_coach_lines(away_text)
    # The secondary referee appears as a clean bare line with no trailing content.
    # Remove it: it's the last entry whose raw line matches the bare pattern exactly.
    away_bare_lines = [
        ln.strip() for ln in away_text.splitlines()
        if _DIGITAL_REF_LINE_RE.match(ln.strip())
    ]
    referee_names = {_DIGITAL_REF_LINE_RE.match(ln).group(1).strip() for ln in away_bare_lines}
    away_coaches = [(n, l) for n, l in away_candidates if n not in referee_names]

    if away_coaches:
        result["away"]["coach"] = {"name": away_coaches[0][0], "license": away_coaches[0][1]}
    if len(away_coaches) >= 2:
        result["away"]["assistant_coach"] = {"name": away_coaches[1][0], "license": away_coaches[1][1]}


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

        # Try Araba scanned format first: LICENSE |NAME DORSAL| entries...
        m = _ARABA_ROSTER_RE.match(line)
        if m:
            name_fragment = m.group("name")
            rest = name_fragment + " " + m.group("rest")
            dorsal_str = m.group("dorsal")
        else:
            # Try Araba digital PDF format: LICENSE NAME DORSAL STATUS...
            m = _ARABA_DIGITAL_ROSTER_RE.match(line)
            if m:
                name_fragment = m.group("name")
                rest = name_fragment + " " + m.group("rest")
                dorsal_str = m.group("dorsal")
            else:
                # Fall back to standard FEB format: DORSAL NAME entries...
                m = _DORSAL_LINE_RE.match(line)
                if not m:
                    continue
                dorsal_str = m.group("dorsal")
                rest = m.group("rest")

        try:
            dorsal = int(dorsal_str)
        except ValueError:
            continue
        if dorsal < 0 or dorsal > 99:
            continue
        if dorsal in seen_dorsals:
            continue

        # A valid roster line should contain a NAME (uppercase letters).
        if not _NAME_RE.search(rest):
            continue

        status = _classify_entry(rest)
        seen_dorsals.add(dorsal)
        entries.append({"dorsal": dorsal, "status": status})

        # Captain marker: ★, (CAP), or " C " near the dorsal/name
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
    # Basque/Araba federation forms use "(CAP)" as the captain marker.
    if re.search(r"\(CAP\)", line, re.IGNORECASE):
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
