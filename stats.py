#!/usr/bin/env python3
"""
Build a normalized stats database from the crawler's output.

Reads:    data/<group-slug>/{group.json, matches.json, matches/*.json}
Writes:   data/<group-slug>/database.json

Output is shaped like normalized database tables, ready to load into a relational
DB or feed a website:

    group                       single object
    teams[]                     {id, name}
    players[]                   {id, team_id, team_name, name, dorsals[]}
    games[]                     {id, jornada, date, home_team_id, away_team_id,
                                  home_score, away_score, quarters, winner, status,
                                  timeouts_home, timeouts_away,
                                  timeouts_by_period_home, timeouts_by_period_away}
    player_game_stats[]         per-player per-game box score + per-quarter breakdown
                                  + foul breakdown (fp_personal, fp_technical,
                                  fp_unsportsmanlike, fp_disqualifying) + fouled_out flag
    log_events[]                normalized play-by-play with foreign keys
    player_season_stats[]       per (player, team) totals + averages + per-quarter avgs
                                  + fouled_out_games counter
    team_season_stats[]         per team totals + averages + per-quarter avgs
                                  + quarters_won/lost/tied + streak info + timeouts_avg
    head_to_head[]              every team-vs-team matchup with aggregate score and W-L
    quarter_leaders[]           top scorers per period (P1..P4, E1)

A player is considered to have NOT PLAYED a game if every box-score stat is zero;
those games are excluded from `games_played`, totals and averages so they don't
pollute season aggregates.

Usage:
    python stats.py data/senior-masculina-3a-grupo-a
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from bs4 import BeautifulSoup

import sys as _sys
import os as _os
_sys.path.insert(0, str(_os.path.dirname(_os.path.abspath(__file__))))
from config import acta_url as _acta_url

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()


def _name_key(name: str) -> str:
    """Canonical identity key for a player name; tolerates casing, punctuation
    and missing commas (the site uses 'Foo, B', 'FOO B.', 'Foo,b' etc.)."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", " ", s).strip().upper()
    return re.sub(r"\s+", " ", s)


_CANON_NAME = re.compile(r"^[A-ZÁÉÍÓÚÑ][\w'\-áéíóúñü]+,\s+[A-ZÁÉÍÓÚÑ]\.?$")


def _best_display_name(variants: set[str], preferred: str | None = None) -> str:
    """Pick the nicest spelling. Prefer the canonical 'Surname, F[.]' form in
    title case; fall back to whichever has a single comma and mixed case.
    `preferred` (the most-recently-seen name) is weighted heavily as the
    second tiebreaker — the latest name in the season usually corrects prior errors."""
    def score(v: str) -> tuple:
        canonical = bool(_CANON_NAME.match(v))
        is_preferred = (v == preferred)
        mixed_case = v != v.upper() and v != v.lower()
        commas = v.count(",")
        good_comma = commas == 1
        return (canonical, is_preferred, mixed_case, good_comma, -abs(commas - 1), len(v))
    return max(variants, key=score)


def _round(v: float, n: int = 2) -> float:
    return round(v, n)


_FT_TL_RE = re.compile(r"\((\d+)\s*TL\)", re.IGNORECASE)


def classify_event(raw: str, fallback_kind: str) -> tuple[str, dict]:
    """Re-classify a log event so we surface foul types the crawler didn't split out.

    Returns (event_kind, extra_fields).
    """
    text = re.sub(r"\s+", " ", (raw or "")).strip().lower()

    if text.startswith("3 punto"):
        return "made_3", {}
    if text.startswith("2 punto"):
        return "made_2", {}
    if text.startswith("tiro libre"):
        m = re.search(r"(\d+)\s*/\s*(\d+)\s+(metido|fallado)", text)
        if m:
            x, y, status = int(m.group(1)), int(m.group(2)), m.group(3)
            return ("ft_made" if status == "metido" else "ft_missed"), {
                "ft_index": x, "ft_of": y,
            }
        return fallback_kind, {}
    if "tiempo muerto" in text:
        return "timeout", {}
    if "fin de periodo" in text:
        return "period_end", {}

    if "antideportiva" in text:
        kind = "foul_unsportsmanlike"
    elif "descalificante" in text:
        kind = "foul_disqualifying"
    elif "falta técnica" in text or "falta tecnica" in text:
        kind = "foul_technical"
    elif "falta personal" in text or text.startswith("falta"):
        kind = "foul_personal"
    else:
        return fallback_kind, {}

    extra: dict = {}
    m = _FT_TL_RE.search(raw or "")
    if m:
        extra["ft_granted"] = int(m.group(1))
    return kind, extra


def _logo_basename(src: str | None) -> str | None:
    if not src:
        return None
    return src.rsplit("/", 1)[-1]


def _build_logo_lookup_from_database(in_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    db_path = in_dir / "database.json"
    if not db_path.exists():
        return {}, {}
    try:
        existing_db = json.loads(db_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}

    by_logo: dict[str, str] = {}
    by_team: dict[str, str] = {}
    for team in existing_db.get("teams", []):
        team_id = team.get("id")
        logo_filename = _logo_basename(team.get("logo_filename"))
        if not team_id or not logo_filename:
            continue
        by_logo.setdefault(logo_filename, team_id)
        by_team.setdefault(team_id, logo_filename)
    return by_logo, by_team


def _build_logo_lookup(in_dir: Path, teams_by_id: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Parse the cached calendar HTML and return two lookups:
       (logo_filename -> team_id, team_id -> logo_filename).

    The crawler currently writes calendario.html to the cache path
    (data/cache/<season>/<group-slug>/raw/). Fall back to the older season
    and legacy flat locations so that logos resolve correctly without
    re-crawling.
    """
    candidates = [
        in_dir / "raw" / "calendario.html",
        in_dir.parent.parent / "cache" / in_dir.parent.name / in_dir.name / "raw" / "calendario.html",
        in_dir.parent.parent / in_dir.name / "raw" / "calendario.html",
    ]
    cal_path = next((path for path in candidates if path.exists()), None)
    if cal_path is None:
        return _build_logo_lookup_from_database(in_dir)
    soup = BeautifulSoup(cal_path.read_text(encoding="utf-8"), "lxml")
    by_logo: dict[str, str] = {}
    by_team: dict[str, str] = {}
    for tr in soup.select("tr.partido"):
        classes = tr.get("class") or []
        ids = [c.replace("Equipo", "") for c in classes if c.startswith("Equipo")]
        tds = tr.find_all("td")
        if len(ids) < 2 or len(tds) < 3:
            continue
        home_img = tds[0].find("img")
        away_img = tds[2].find("img")
        if home_img and home_img.get("src"):
            fn = _logo_basename(home_img["src"])
            by_logo[fn] = ids[0]
            by_team.setdefault(ids[0], fn)
        if away_img and away_img.get("src"):
            fn = _logo_basename(away_img["src"])
            by_logo[fn] = ids[1]
            by_team.setdefault(ids[1], fn)
    return by_logo, by_team


def is_played(p: dict) -> bool:
    """A player is considered to have played if any box-score stat is non-zero."""
    return any(p[k] for k in ("pts", "t2", "t3", "tl_made", "tl_att", "fp"))


# ---------------------------------------------------------------------------
# Phase 4 helpers — starter / DNP derivation and club grouping
# ---------------------------------------------------------------------------

def _derive_starters_for_game(log_events: list[dict], side: str) -> tuple[list[str], str]:
    """Infer the 5 starters for one team-side from the play-by-play log of a game.

    The PBP log inside matches/<id>.json is ordered clock-descending within each
    period: the LAST entry of period P1 in the array is the FIRST tip-off event
    chronologically. We iterate `reversed(p1_events)` to obtain chronological
    order and pick the first 5 distinct dorsals for the requested side.

    If P1 yields fewer than 5 distinct dorsals (sparse PBP), we continue
    scanning subsequent periods in chronological order until we reach 5.
    The earliest-appearing dorsal across all periods is the best proxy for a
    starter when the log is incomplete.

    Returns (dorsals_in_order, source) where source is:
        "pbp"       — 5 distinct dorsals found entirely within P1
        "pbp_padded" — completed using events from later periods
        "none"      — log has no events for this side at all
    """
    PERIOD_ORDER = ["P1", "P2", "P3", "P4", "E1", "E2", "E3"]

    # Collect events grouped by period, preserving chronological order within each
    by_period: dict[str, list[dict]] = {}
    for e in log_events:
        p = e.get("period")
        if p:
            by_period.setdefault(p, []).append(e)

    # Sort periods by canonical order (unknown periods go last)
    ordered_periods = sorted(
        by_period.keys(),
        key=lambda p: PERIOD_ORDER.index(p) if p in PERIOD_ORDER else 99,
    )

    seen: list[str] = []
    p1_complete = False
    for period in ordered_periods:
        events = by_period[period]
        for e in reversed(events):  # reversed = chronological (log is clock-desc)
            if e.get("side") != side:
                continue
            dorsal = e.get("player_dorsal")
            if not dorsal or dorsal in seen:
                continue
            seen.append(dorsal)
            if len(seen) >= 5:
                break
        if period == "P1" and len(seen) >= 5:
            p1_complete = True
        if len(seen) >= 5:
            break

    if not seen or len(seen) < 5:
        return [], "none"
    if p1_complete:
        return seen, "pbp"
    return seen, "pbp_padded"


def _classify_dnp_for_player_game(
    player_box: dict,
    log_events: list[dict],
    side: str,
) -> bool:
    """A player DNP'd this game iff their box-score is all zeros AND they never
    appear as `player_dorsal` in any PBP event for their side.
    """
    if is_played(player_box):
        return False
    dorsal = player_box.get("dorsal")
    if not dorsal:
        # Without a dorsal we can't safely cross-check PBP — fall back to "all
        # zeros == DNP".
        return True
    for e in log_events:
        if e.get("side") != side:
            continue
        if e.get("player_dorsal") == dorsal:
            return False
    return True


# ---------------------------------------------------------------------------
# Phase 5: parsed-acta helpers (officials / coaches / captains)
# ---------------------------------------------------------------------------

def _load_parsed_acta(in_dir: Path, partido_id: str) -> dict | None:
    """Load `<in_dir>/raw/acta_<partido_id>.parsed.json` if present and valid.

    Returns the parsed dict or None when the file is missing or malformed.
    Never raises — a bad acta is just absent metadata.
    """
    if not partido_id:
        return None
    path = in_dir / "raw" / f"acta_{partido_id}.parsed.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not load parsed acta for %s: %s", partido_id, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _canonical_person_id(name: str | None, license_: str | None,
                         prefix: str) -> str | None:
    """Build a canonical person id. Prefers `<prefix>__lic_<license>` when a
    license is present; falls back to `<prefix>__<slug(name_key)>` otherwise.

    Returns None when both name and license are missing/blank.
    """
    if license_:
        lic = re.sub(r"[^A-Za-z0-9]+", "", license_).upper()
        # Reject pathological sentinels that would silently merge unrelated people.
        if lic and len(lic) >= 3 and lic not in {"NA", "SL", "SIN", "NONE", "NULL", "000"}:
            return f"{prefix}__lic_{lic}"
    if name:
        nk = _name_key(name)
        if nk:
            slug = re.sub(r"[^A-Za-z0-9]+", "-", nk).strip("-").lower()
            if slug:
                return f"{prefix}__{slug}"
    return None


def _compute_acta_confidence(parsed: dict) -> str:
    """Classify parsed-acta quality.

    For digital PDFs (``digital_pdf_format`` in warnings): only referees are
    extracted — coaches and player entries are not available from the digital
    format.  Confidence is "ok" when at least one referee name was found, and
    "low" only when *both* referee fields are empty.

    For scanned/OCR'd PDFs (no ``digital_pdf_format`` warning): "low" iff
    ``warnings`` is non-empty AND any of:
      - both referees (principal + auxiliar) have no name
      - both teams' coaches have no name
      - both teams' ``entries`` lists are empty
    Otherwise "ok".
    """
    warnings = parsed.get("warnings") or []
    officials = parsed.get("officials") or {}
    ref_p = (officials.get("referee_principal") or {}).get("name")
    ref_a = (officials.get("referee_auxiliar") or {}).get("name")
    refs_missing = not ref_p and not ref_a

    if "digital_pdf_format" in warnings:
        # Digital PDFs don't have coaches or entries; judge only on referees.
        return "low" if refs_missing else "ok"

    if not warnings:
        return "ok"

    home = parsed.get("home") or {}
    away = parsed.get("away") or {}
    coach_h = (home.get("coach") or {}).get("name")
    coach_a = (away.get("coach") or {}).get("name")
    coaches_missing = not coach_h and not coach_a

    entries_h = home.get("entries") or []
    entries_a = away.get("entries") or []
    entries_missing = not entries_h and not entries_a

    if refs_missing or coaches_missing or entries_missing:
        return "low"
    return "ok"


_TRAILING_LETTER_RE = re.compile(r"\s+([A-Z])\s*$")
_TRAILING_COLOR_RE = re.compile(
    r"\s+(AZUL|GRANA|ROJO|ROJA|BLANCO|BLANCA|NEGRO|NEGRA|VERDE|NARANJA|AMARILLO|AMARILLA|ORO|PLATA)\s*$",
    re.IGNORECASE,
)


def _club_root_name(team_name: str) -> str:
    """Strip trailing color word (Azul, Grana…) or single letter (A/B/C…) from a team name."""
    name = (team_name or "").strip()
    name = _TRAILING_COLOR_RE.sub("", name).strip()
    name = _TRAILING_LETTER_RE.sub("", name).strip()
    return name or (team_name or "").strip()


def _logo_root(filename: str | None) -> str | None:
    """Strip a trailing _a / _b / _c suffix from a logo filename before its
    extension. Used as a secondary tie-break for club grouping."""
    if not filename:
        return None
    name = filename.rsplit(".", 1)[0]
    return re.sub(r"_[a-z]$", "", name.lower())


def _group_clubs(teams: list[dict]) -> list[dict]:
    """Group teams into clubs using the longest-common-prefix heuristic.

    Strategy:
      1. Strip a trailing single-letter token (' A', ' B', ' C'…) from each
         team name. The remainder is the candidate club name.
      2. Teams with the same candidate club name share a club.
      3. Single-team clubs keep the original (un-stripped) name when no
         trailing-letter token was present.
      4. As a secondary tie-break, teams whose logo filenames share the same
         root (after stripping `_a`/`_b`/… suffix) are merged with the team
         that already owns that root, even if their prefixes differ slightly.

    Returns a list of clubs sorted by name asc:
        [{"id": slug, "name": str, "team_ids": [..]}, ...]
    """
    if not teams:
        return []

    # Step 1: bucket by stripped-prefix name.
    by_root: dict[str, list[dict]] = defaultdict(list)
    for t in teams:
        root = _club_root_name(t.get("name") or "")
        by_root[root].append(t)

    # Step 2: secondary tie-break via logo-root.
    # Only merge logo-root matches when one side has a stripped-letter prefix
    # (i.e. the prefix bucket would otherwise have it as a 1-team club).
    logo_owner: dict[str, str] = {}  # logo_root -> primary key in by_root
    for root in sorted(by_root.keys()):
        members = by_root.get(root)
        if not members:
            continue
        for m in sorted(members, key=lambda x: x.get("id") or ""):
            lr = _logo_root(m.get("logo_filename"))
            if not lr:
                continue
            if lr in logo_owner and logo_owner[lr] != root:
                # Merge this root into the existing owner.
                target = logo_owner[lr]
                if root in by_root and root != target:
                    by_root[target].extend(by_root[root])
                    del by_root[root]
                    break
            else:
                logo_owner[lr] = root

    clubs: list[dict] = []
    for root, members in by_root.items():
        # Use the longest member name as a stable display label when the
        # stripped root is empty (defensive — shouldn't happen for real data).
        name = root or max((m.get("name") or "" for m in members), key=len)
        clubs.append({
            "id": _slug(name),
            "name": name,
            "team_ids": sorted(m["id"] for m in members),
        })
    clubs.sort(key=lambda c: c["name"])
    return clubs


def _aggregate_club_season_stats(
    clubs: list[dict],
    team_season_stats: list[dict],
    players: list[dict],
    games: list[dict],
) -> list[dict]:
    """Build per-club aggregate season stats.

    Sums child teams' team_season_stats numeric fields. Re-derives win_pct and
    averages from the aggregated totals. Player count is derived from the
    flat players[] table (each player is unique by (team_id, name_key)).
    """
    ts_by_team = {ts["team_id"]: ts for ts in team_season_stats}
    players_by_team: dict[str, int] = defaultdict(int)
    for p in players:
        players_by_team[p.get("team_id")] += 1

    out: list[dict] = []
    for club in clubs:
        tids = set(club["team_ids"])
        gp = wins = losses = draws = 0
        pf = pa = 0
        for tid in tids:
            ts = ts_by_team.get(tid)
            if not ts:
                continue
            gp += ts.get("games_played", 0) or 0
            wins += ts.get("wins", 0) or 0
            losses += ts.get("losses", 0) or 0
            draws += ts.get("draws", 0) or 0
            pf += ts.get("points_for", 0) or 0
            pa += ts.get("points_against", 0) or 0
        players_count = sum(players_by_team.get(tid, 0) for tid in tids)
        out.append({
            "club_id": club["id"],
            "teams_count": len(tids),
            "players_count": players_count,
            "games_played": gp,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "win_pct": _round(wins / gp, 3) if gp else 0,
            "points_for": pf,
            "points_against": pa,
            "point_diff": pf - pa,
            "avg_points_for": _round(pf / gp) if gp else 0,
            "avg_points_against": _round(pa / gp) if gp else 0,
        })
    out.sort(key=lambda r: (-r["win_pct"], -r["point_diff"]))
    return out


def _is_abbreviation(short: str, long: str) -> bool:
    """Return True if every token in `short` maps to a token in `long`:
    single-letter tokens must match the first letter of a longer token;
    multi-letter tokens must match or be a prefix (≥3 chars) of a longer token.
    Tokens are matched greedily left-to-right without reuse."""
    s_tokens = short.split()
    l_tokens = long.split()
    if len(s_tokens) > len(l_tokens):
        return False
    used: set[int] = set()
    for st in s_tokens:
        matched = False
        for idx, lt in enumerate(l_tokens):
            if idx in used:
                continue
            if len(st) == 1:
                if lt[0] == st:
                    used.add(idx)
                    matched = True
                    break
            else:
                if lt == st or (len(st) >= 3 and lt.startswith(st[:3])):
                    used.add(idx)
                    matched = True
                    break
        if not matched:
            return False
    return True


def _merge_name_aliases(
    players: dict,
    player_game_stats: list[dict],
    threshold: float = 0.80,
) -> dict[str, str]:
    """Merge likely-typo duplicates: same team, shared dorsal, compatible initials,
    and either name-key similarity >= threshold OR one name is an abbreviation of
    the other. Returns old_id -> canonical_id redirect map.
    Mutates `players` in-place (removes alias entries, merges their variants/dorsals
    into the canonical entry)."""

    gp: dict[str, int] = defaultdict(int)
    for r in player_game_stats:
        if r["played"]:
            gp[r["player_id"]] += 1

    def _initials(nk: str) -> set[str]:
        return {t for t in nk.split() if len(t) == 1}

    def _compatible(nk1: str, nk2: str) -> bool:
        i1, i2 = _initials(nk1), _initials(nk2)
        # If either name has no parseable initial we can't rule it out.
        if not i1 or not i2:
            return True
        return bool(i1 & i2)

    def _should_merge(nk1: str, nk2: str) -> bool:
        if SequenceMatcher(None, nk1, nk2).ratio() >= threshold:
            return True
        # Detect full-name vs initial pattern (e.g. "MIRANDA GABINA JUNE" vs "MIRANDA J").
        t1, t2 = nk1.split(), nk2.split()
        shorter, longer = (nk1, nk2) if len(t1) <= len(t2) else (nk2, nk1)
        return _is_abbreviation(shorter, longer)

    by_team: dict[str, list] = defaultdict(list)
    for key in players:
        by_team[key[0]].append(key)

    parent: dict[str, str] = {}

    def find(pid: str) -> str:
        while pid in parent:
            pid = parent[pid]
        return pid

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Canonical = more games played (more data = more authoritative name).
        if gp.get(ra, 0) >= gp.get(rb, 0):
            parent[rb] = ra
        else:
            parent[ra] = rb

    for team_keys in by_team.values():
        entries = [(k, players[k]) for k in team_keys]
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                ki, info_i = entries[i]
                kj, info_j = entries[j]
                if not (info_i["dorsals"] & info_j["dorsals"]):
                    continue
                if not _compatible(ki[1], kj[1]):
                    continue
                if not _should_merge(ki[1], kj[1]):
                    continue
                union(info_i["id"], info_j["id"])

    redirect: dict[str, str] = {}
    id_to_key = {info["id"]: k for k, info in players.items()}
    to_delete: list = []
    for k, info in list(players.items()):
        root = find(info["id"])
        if root != info["id"]:
            redirect[info["id"]] = root
            canon_key = id_to_key[root]
            players[canon_key]["name_variants"] |= info["name_variants"]
            players[canon_key]["dorsals"] |= info["dorsals"]
            # Propagate the more recently seen name to the canonical entry.
            if info.get("last_seq", 0) > players[canon_key].get("last_seq", 0):
                players[canon_key]["name"] = info["name"]
                players[canon_key]["last_seq"] = info["last_seq"]
            to_delete.append(k)
    for k in to_delete:
        del players[k]
    return redirect


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

@dataclass
class TeamRef:
    id: str
    name: str


def build_database(in_dir: Path) -> dict:
    group_meta = json.loads((in_dir / "group.json").read_text(encoding="utf-8"))
    index = json.loads((in_dir / "matches.json").read_text(encoding="utf-8"))

    teams_by_id: dict[str, str] = dict(group_meta["teams"])  # id -> name
    teams_by_name: dict[str, str] = {v: k for k, v in teams_by_id.items()}
    logo_to_team_id, team_id_to_logo = _build_logo_lookup(in_dir, teams_by_id)

    def resolve_team_id(name: str | None, logo: str | None) -> str | None:
        # Primary: canonical calendar name. Fallback: logo filename (source data
        # sometimes uses alternate sponsor / typo-ed display names in box scores).
        if name and name in teams_by_name:
            return teams_by_name[name]
        if logo:
            tid = logo_to_team_id.get(_logo_basename(logo))
            if tid:
                return tid
        return None

    # registries
    players: dict[tuple[str, str], dict] = {}      # (team_id, name) -> player info
    games: list[dict] = []
    player_game_stats: list[dict] = []
    log_events: list[dict] = []
    _seq = [0]  # global call counter to track which name was seen most recently

    def get_player_id(team_id: str, name: str, dorsal: str | None) -> str:
        _seq[0] += 1
        key = (team_id, _name_key(name))
        if key not in players:
            players[key] = {
                "id": f"{team_id}__{_slug(name)}",
                "team_id": team_id,
                "team_name": teams_by_id.get(team_id, team_id),
                "name": name,
                "name_variants": {name},
                "dorsals": set(),
                "last_seq": _seq[0],
            }
        else:
            players[key]["name_variants"].add(name)
            players[key]["name"] = name        # update to most-recently-seen spelling
            players[key]["last_seq"] = _seq[0]
        if dorsal:
            players[key]["dorsals"].add(dorsal)
        return players[key]["id"]

    # ---- iterate matches in the index ----
    for m in index["matches"]:
        pid = m.get("partido_id") or ""
        if not pid:
            # Skip bracket slots where both participant names are still placeholders
            # (e.g. "1º GRUPO X" / "GANADOR 1") — these are unresolved seeds that
            # have no real score and would appear as 0-0 "FINALIZADO" in the UI.
            home_name = m.get("home_team") or ""
            away_name = m.get("away_team") or ""
            if _is_placeholder(home_name) or _is_placeholder(away_name):
                continue
            # Walkover / not played yet — still record a games row with what we have,
            # but skip per-player stats and log. Prefer the team_ids the crawler
            # attached; fall back to name+logo resolution.
            home_id = m.get("home_team_id") or resolve_team_id(home_name, m.get("home_logo"))
            away_id = m.get("away_team_id") or resolve_team_id(away_name, m.get("away_logo"))
            no_pid_date = None
            if m.get("starts_at"):
                for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
                    try:
                        no_pid_date = datetime.strptime(m["starts_at"], fmt).isoformat()
                        break
                    except ValueError:
                        pass
            games.append({
                "id": None,
                "jornada": m["jornada"],
                "date": no_pid_date,
                "venue": m.get("venue"),
                "status": m.get("status"),
                "home_team_id": home_id,
                "away_team_id": away_id,
                "home_score": m.get("home_score"),
                "away_score": m.get("away_score"),
                "winner": _winner(m.get("home_score"), m.get("away_score"), m.get("status")),
                "quarters": [],
                "has_box_score": False,
                "timeouts_home": None,
                "timeouts_away": None,
                "timeouts_by_period_home": None,
                "timeouts_by_period_away": None,
                "starters_home": [],
                "starters_away": [],
            })
            continue

        match_path = in_dir / "matches" / f"{pid}.json"
        if not match_path.exists():
            # partido_id known but no detail file yet — record a calendar-style row
            # with has_box_score=False and the acta_url for future scraping
            home_id = m.get("home_team_id") or resolve_team_id(m.get("home_team"), m.get("home_logo"))
            away_id = m.get("away_team_id") or resolve_team_id(m.get("away_team"), m.get("away_logo"))
            no_box_date = None
            if m.get("starts_at"):
                for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
                    try:
                        no_box_date = datetime.strptime(m["starts_at"], fmt).isoformat()
                        break
                    except ValueError:
                        pass
            games.append({
                "id": pid,
                "jornada": m["jornada"],
                "date": no_box_date,
                "venue": m.get("venue"),
                "status": m.get("status"),
                "home_team_id": home_id,
                "away_team_id": away_id,
                "home_score": m.get("home_score"),
                "away_score": m.get("away_score"),
                "winner": _winner(m.get("home_score"), m.get("away_score"), m.get("status")),
                "quarters": [],
                "has_box_score": False,
                "acta_url": _acta_url(pid),
                "timeouts_home": None,
                "timeouts_away": None,
                "timeouts_by_period_home": None,
                "timeouts_by_period_away": None,
                "starters_home": [],
                "starters_away": [],
            })
            continue
        detail = json.loads(match_path.read_text(encoding="utf-8"))

        # Only process box scores for completed games. If the detail file was captured
        # while the game was live (P1-P4, SIN EMPEZAR, etc.) the scores are partial
        # and player stats are unreliable. Treat as no-box-score; the crawler will
        # overwrite the file once the acta is published.
        _COMPLETED_STATUSES = {"FINALIZADO", "SUSPENDIDO", "CANCELADO"}
        detail_status = detail.get("status")
        if detail_status not in _COMPLETED_STATUSES:
            home_id = m.get("home_team_id") or resolve_team_id(detail["home"]["team"], detail["home"].get("logo"))
            away_id = m.get("away_team_id") or resolve_team_id(detail["away"]["team"], detail["away"].get("logo"))
            no_box_date = None
            if m.get("starts_at"):
                for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
                    try:
                        no_box_date = datetime.strptime(m["starts_at"], fmt).isoformat()
                        break
                    except ValueError:
                        pass
            games.append({
                "id": pid,
                "jornada": m["jornada"],
                "date": no_box_date,
                "venue": m.get("venue"),
                "status": detail_status,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "home_score": None,
                "away_score": None,
                "winner": None,
                "quarters": [],
                "has_box_score": False,
                "acta_url": _acta_url(pid),
                "timeouts_home": None,
                "timeouts_away": None,
                "timeouts_by_period_home": None,
                "timeouts_by_period_away": None,
                "starters_home": [],
                "starters_away": [],
            })
            continue

        # Phase 5 — parsed acta sidecar (officials / coaches / captains / notes).
        parsed_acta = _load_parsed_acta(in_dir, pid)

        home_team_id = resolve_team_id(detail["home"]["team"], detail["home"].get("logo"))
        away_team_id = resolve_team_id(detail["away"]["team"], detail["away"].get("logo"))
        if not home_team_id:
            log.warning("Could not resolve home team_id for match %s (name=%r)", pid, detail["home"]["team"])
        if not away_team_id:
            log.warning("Could not resolve away team_id for match %s (name=%r)", pid, detail["away"]["team"])

        date_iso = None
        if m.get("starts_at"):
            for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
                try:
                    date_iso = datetime.strptime(m["starts_at"], fmt).isoformat()
                    break
                except ValueError:
                    pass

        home_score = detail["home"]["total_pts"]
        away_score = detail["away"]["total_pts"]
        game_row = {
            "id": pid,
            "jornada": m["jornada"],
            "date": date_iso,
            "venue": m.get("venue"),
            "status": detail["status"],
            "home_team_id": home_team_id,
            "away_team_id": away_team_id,
            "home_score": home_score,
            "away_score": away_score,
            "winner": _winner(home_score, away_score, detail["status"]),
            "quarters": detail["quarters"],
            "has_box_score": True,
            "acta_url": _acta_url(pid),
        }
        if detail.get("cronica_es"):
            game_row["cronica_es"] = detail["cronica_es"]
        if detail.get("cronica_eu"):
            game_row["cronica_eu"] = detail["cronica_eu"]
        games.append(game_row)

        # ---- per-quarter aggregation from the play-by-play log ----
        # name -> period -> dict(pts, t2, t3, tl_made, tl_att, fp)
        per_player_q: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: {"pts": 0, "t2": 0, "t3": 0, "tl_made": 0, "tl_att": 0, "fp": 0})
        )
        # name -> dict(fp_personal, fp_technical, fp_unsportsmanlike, fp_disqualifying)
        per_player_fouls: dict = defaultdict(lambda: {
            "fp_personal": 0,
            "fp_technical": 0,
            "fp_unsportsmanlike": 0,
            "fp_disqualifying": 0,
        })
        # (team_id, name_key) -> fp_received count (fouls drawn that generated free throws)
        per_player_fp_received: dict = defaultdict(int)

        # Pre-pass: for each foul event that grants TL, find the recipient from the
        # preceding ft_made/ft_missed entries with the same clock+period on the opposite side.
        # The log is stored reverse-chronologically, so TL entries precede (lower index)
        # the foul event that triggered them.
        _log = detail["log"]
        for _i, _ev in enumerate(_log):
            _raw = _ev.get("event", "")
            _kind, _extra = classify_event(_raw, _ev.get("event_kind", "other"))
            if not _kind.startswith("foul_"):
                continue
            _ft_granted = _extra.get("ft_granted", 0)
            if not _ft_granted:
                continue
            _foul_side = _ev.get("side")
            _recv_side = "away" if _foul_side == "home" else ("home" if _foul_side == "away" else None)
            if not _recv_side:
                continue
            _clock = _ev.get("clock")
            _period = _ev.get("period")
            # Scan backwards (lower indices = earlier in reverse log = closer in time)
            for _j in range(_i - 1, max(_i - (_ft_granted * 2 + 3), -1), -1):
                _prev = _log[_j]
                if _prev.get("clock") != _clock or _prev.get("period") != _period:
                    break
                _pk, _ = classify_event(_prev.get("event", ""), _prev.get("event_kind", "other"))
                if _pk in ("ft_made", "ft_missed") and _prev.get("side") == _recv_side:
                    _rname = _prev.get("player_name")
                    _rdorsal = _prev.get("player_dorsal")
                    _rteam = home_team_id if _recv_side == "home" else away_team_id
                    if _rname and _rteam:
                        per_player_fp_received[(_rteam, _name_key(_rname))] += 1
                    break
        # B5 — timeouts per side and per period for this game
        timeouts_total = {"home": 0, "away": 0}
        timeouts_by_period: dict[str, dict[str, int]] = {"home": defaultdict(int), "away": defaultdict(int)}

        for seq, e in enumerate(detail["log"]):
            kind, extra = classify_event(e.get("event", ""), e.get("event_kind", "other"))

            side = e["side"]
            team_id_ev = home_team_id if side == "home" else (away_team_id if side == "away" else None)
            pname = e.get("player_name")
            pdorsal = e.get("player_dorsal")
            player_ref = None
            if pname and team_id_ev:
                player_ref = get_player_id(team_id_ev, pname, pdorsal)

            period = e["period"]
            if kind == "timeout" and side in ("home", "away") and period:
                timeouts_total[side] += 1
                timeouts_by_period[side][period] += 1
            if pname and team_id_ev and period:
                bucket = per_player_q[(team_id_ev, _name_key(pname))][period]
                if kind == "made_2":
                    bucket["pts"] += 2
                    bucket["t2"] += 1
                elif kind == "made_3":
                    bucket["pts"] += 3
                    bucket["t3"] += 1
                elif kind == "ft_made":
                    bucket["pts"] += 1
                    bucket["tl_made"] += 1
                    bucket["tl_att"] += 1
                elif kind == "ft_missed":
                    bucket["tl_att"] += 1
                elif kind.startswith("foul_"):
                    # FIBA: only personal / unsportsmanlike / disqualifying fouls count
                    # toward the 5-foul ejection. Pure technicals do NOT.
                    if kind in ("foul_personal", "foul_unsportsmanlike", "foul_disqualifying"):
                        bucket["fp"] += 1
                    fk = per_player_fouls[(team_id_ev, _name_key(pname))]
                    if kind == "foul_personal":
                        fk["fp_personal"] += 1
                    elif kind == "foul_technical":
                        fk["fp_technical"] += 1
                    elif kind == "foul_unsportsmanlike":
                        fk["fp_unsportsmanlike"] += 1
                    elif kind == "foul_disqualifying":
                        fk["fp_disqualifying"] += 1

            log_events.append({
                "game_id": pid,
                "seq": seq,
                "period": period,
                "clock": e.get("clock"),
                "side": side,
                "team_id": team_id_ev,
                "player_id": player_ref,
                "player_name": pname,
                "player_dorsal": pdorsal,
                "event": e.get("event"),
                "event_kind": kind,
                "ft_index": extra.get("ft_index") or e.get("ft_index"),
                "ft_of": extra.get("ft_of") or e.get("ft_of"),
                "ft_granted": extra.get("ft_granted"),
                "score_home": e.get("score_home"),
                "score_away": e.get("score_away"),
            })

        # ---- Phase 4: derive starters per side from PBP ----
        home_starter_dorsals, home_starter_src = _derive_starters_for_game(detail["log"], "home")
        away_starter_dorsals, away_starter_src = _derive_starters_for_game(detail["log"], "away")
        starter_sources = {"home": home_starter_src, "away": away_starter_src}
        starter_dorsals_by_side = {"home": set(home_starter_dorsals), "away": set(away_starter_dorsals)}
        # Resolve dorsals → player_ids for the games[] row (filled after we
        # know each player's id below).
        starters_player_ids: dict[str, list[str]] = {"home": [], "away": []}

        # Phase 5 — per-side dorsal→player_id maps live OUTSIDE the loop so
        # captain dorsal resolution (below) can see both sides.
        dorsal_to_pid_by_side: dict[str, dict[str, str]] = {"home": {}, "away": {}}

        # ---- per-team box scores ----
        pgs_start_idx = len(player_game_stats)  # B7: index before this game's rows
        for side_label, side_team_id, team_box in [
            ("home", home_team_id, detail["home"]),
            ("away", away_team_id, detail["away"]),
        ]:
            # Per-side maps from dorsal → player_id for starter id resolution.
            dorsal_to_pid: dict[str, str] = dorsal_to_pid_by_side[side_label]
            for p in team_box["players"]:
                player_id = get_player_id(side_team_id, p["name"], p.get("dorsal"))
                played = is_played(p)
                by_quarter = {}
                if played:
                    by_quarter = {
                        period: dict(stats)
                        for period, stats in sorted(per_player_q[(side_team_id, _name_key(p["name"]))].items())
                    }
                foul_breakdown = per_player_fouls[(side_team_id, _name_key(p["name"]))]
                fp_personal = foul_breakdown["fp_personal"]
                fp_technical = foul_breakdown["fp_technical"]
                fp_unsportsmanlike = foul_breakdown["fp_unsportsmanlike"]
                fp_disqualifying = foul_breakdown["fp_disqualifying"]
                # B6 — fouled_out (FIBA): 5 personal fouls, 1 disqualifying,
                # 2 unsportsmanlikes, or 2 technicals.
                fouled_out = (
                    p["fp"] >= 5
                    or fp_disqualifying >= 1
                    or fp_unsportsmanlike >= 2
                    or fp_technical >= 2
                )
                # Phase 4 — DNP / starter derivation per (game, player).
                dnp = _classify_dnp_for_player_game(p, detail["log"], side_label)
                starter_src = starter_sources[side_label]
                is_starter = (
                    starter_src in ("pbp", "pbp_padded")
                    and not dnp
                    and (p.get("dorsal") in starter_dorsals_by_side[side_label])
                )
                if p.get("dorsal"):
                    dorsal_to_pid[p["dorsal"]] = player_id
                fp_received = per_player_fp_received.get((side_team_id, _name_key(p["name"])), 0)
                player_game_stats.append({
                    "game_id": pid,
                    "player_id": player_id,
                    "team_id": side_team_id,
                    "side": side_label,
                    "dorsal": p.get("dorsal"),
                    "pts": p["pts"], "t2": p["t2"], "t3": p["t3"],
                    "tl_made": p["tl_made"], "tl_att": p["tl_att"], "fp": p["fp"],
                    "fp_personal": fp_personal,
                    "fp_technical": fp_technical,
                    "fp_unsportsmanlike": fp_unsportsmanlike,
                    "fp_disqualifying": fp_disqualifying,
                    "fp_received": fp_received,
                    "ft_pct": _round(p["tl_made"] / p["tl_att"], 3) if p["tl_att"] else None,
                    "played": played,
                    "fouled_out": fouled_out,
                    "by_quarter": by_quarter,
                    "dnp": dnp,
                    "starter": is_starter,
                    "starter_source": starter_src,
                })

            # After the side's roster loop: resolve starter dorsals to player_ids
            # in the same chronological order as the PBP.
            ordered = home_starter_dorsals if side_label == "home" else away_starter_dorsals
            starters_player_ids[side_label] = [
                dorsal_to_pid[d] for d in ordered if d in dorsal_to_pid
            ]

        # B7 — compute plus/minus and estimated minutes for this game's rows.
        this_game_pgs = player_game_stats[pgs_start_idx:]
        side_to_pgs: dict[str, list[dict]] = {"home": [], "away": []}
        for row in this_game_pgs:
            side_to_pgs[row["side"]].append(row)
        _derive_plus_minus_per_game(detail["log"], side_to_pgs)

        # B5 — write the collected timeouts to this game's row.
        game_row["timeouts_home"] = timeouts_total["home"]
        game_row["timeouts_away"] = timeouts_total["away"]
        game_row["timeouts_by_period_home"] = dict(timeouts_by_period["home"])
        game_row["timeouts_by_period_away"] = dict(timeouts_by_period["away"])
        # Phase 4 — starters derived from PBP (empty list when source is "none").
        game_row["starters_home"] = list(starters_player_ids["home"])
        game_row["starters_away"] = list(starters_player_ids["away"])

        # Phase 5 — attach parsed-acta sidecar fields (only if present).
        if parsed_acta:
            confidence = _compute_acta_confidence(parsed_acta)
            officials_raw = parsed_acta.get("officials") or {}
            officials_out: dict[str, dict] = {}
            for role, person in officials_raw.items():
                if not isinstance(person, dict):
                    continue
                name = person.get("name")
                license_ = person.get("license")
                if not name and not license_:
                    continue
                officials_out[role] = {
                    "name": name,
                    "license": license_,
                    "id": _canonical_person_id(name, license_, "ref"),
                }

            coaches_out: dict[str, dict] = {}
            for side_key, side_team_id in [("home", home_team_id), ("away", away_team_id)]:
                side_blob = parsed_acta.get(side_key) or {}
                head = side_blob.get("coach") or {}
                assistant = side_blob.get("assistant_coach") or {}
                head_name = head.get("name")
                head_lic = head.get("license")
                assist_name = assistant.get("name")
                assist_lic = assistant.get("license")
                if not head_name and not head_lic and not assist_name and not assist_lic:
                    continue
                coaches_out[side_key] = {
                    "team_id": side_team_id,
                    "head": {
                        "name": head_name,
                        "license": head_lic,
                        "id": _canonical_person_id(head_name, head_lic, "coach"),
                    } if (head_name or head_lic) else None,
                    "assistant": {
                        "name": assist_name,
                        "license": assist_lic,
                        "id": _canonical_person_id(assist_name, assist_lic, "coach"),
                    } if (assist_name or assist_lic) else None,
                }

            captains_out: dict[str, dict] = {}
            for side_key in ("home", "away"):
                side_blob = parsed_acta.get(side_key) or {}
                cap_dorsal = side_blob.get("captain_dorsal")
                if not cap_dorsal:
                    continue
                pid_resolved = dorsal_to_pid_by_side[side_key].get(str(cap_dorsal))
                if not pid_resolved:
                    log.debug(
                        "captain dorsal %s on %s side did not resolve to a player_id in game %s",
                        cap_dorsal, side_key, game_row.get("id"),
                    )
                captains_out[side_key] = {
                    "dorsal": str(cap_dorsal),
                    f"{side_key}_player_id": pid_resolved,
                    "player_id": pid_resolved,
                }

            game_row["officials"] = officials_out or None
            game_row["coaches"] = coaches_out or None
            game_row["captains"] = captains_out or None
            game_row["attendance"] = parsed_acta.get("attendance")
            game_row["notes"] = parsed_acta.get("notes")
            game_row["acta_pdf_confidence"] = confidence

    # ---- enrich games with q1_winner and best scoring runs ----
    _log_by_game: dict[str, list[dict]] = defaultdict(list)
    for e in log_events:
        gid = e.get("game_id")
        if gid:
            _log_by_game[gid].append(e)
    for g in games:
        quarters = g.get("quarters") or []
        if len(quarters) >= 1:
            qh, qa = quarters[0]
            if qh > qa:
                g["q1_winner"] = "home"
            elif qa > qh:
                g["q1_winner"] = "away"
            else:
                g["q1_winner"] = "tie"
        else:
            g["q1_winner"] = None
        gid = g.get("id")
        if gid and gid in _log_by_game:
            best_home, best_away = _best_scoring_run_per_game(_log_by_game[gid])
            g["best_run_home"] = best_home
            g["best_run_away"] = best_away
        else:
            g["best_run_home"] = None
            g["best_run_away"] = None

    # ---- merge typo duplicates (same team+dorsal, similar name, compatible initials) ----
    redirect = _merge_name_aliases(players, player_game_stats)
    if redirect:
        for r in player_game_stats:
            if r["player_id"] in redirect:
                r["player_id"] = redirect[r["player_id"]]
        for e in log_events:
            if e.get("player_id") and e["player_id"] in redirect:
                e["player_id"] = redirect[e["player_id"]]
        for g in games:
            if g.get("starters_home"):
                g["starters_home"] = [redirect.get(pid, pid) for pid in g["starters_home"]]
            if g.get("starters_away"):
                g["starters_away"] = [redirect.get(pid, pid) for pid in g["starters_away"]]
            # Phase 5 — captain player_ids must follow the same redirect.
            caps = g.get("captains")
            if caps:
                for side_key, cap in caps.items():
                    for k in ("player_id", f"{side_key}_player_id"):
                        if cap.get(k) and cap[k] in redirect:
                            cap[k] = redirect[cap[k]]

    # ---- season aggregates per player (only games they actually played) ----
    # Build game_winner_map for foul-out impact tracking
    _game_winner_map: dict[str, str | None] = {
        g["id"]: g.get("winner") for g in games if g.get("id")
    }
    player_season = _player_season_stats(player_game_stats, log_events, _game_winner_map)

    # ---- season aggregates per team ----
    team_season = _team_season_stats(games, player_game_stats, log_events)
    # Phase 4 — augment team_season_stats with dnp_total (count of DNP rows per team).
    dnp_total_by_team: dict[str, int] = defaultdict(int)
    for r in player_game_stats:
        if r.get("dnp") and r.get("team_id"):
            dnp_total_by_team[r["team_id"]] += 1
    for ts in team_season:
        ts["dnp_total"] = dnp_total_by_team.get(ts["team_id"], 0)

    # ---- Phase 4: clubs (sibling-team groupings) and club season stats ----
    # Drop placeholder teams the source creates for unfinished brackets:
    #   - "GANADOR 1", "PERDEDOR 2", "VENCEDOR X", "EQUIPO 3"
    #   - "1º GRUPO X", "2º GRUPO Y" (bracket seed slots not yet resolved)
    # These either never appear in a real game, or only appear in pending
    # (SIN EMPEZAR / non-played) games. Once the bracket resolves, the
    # source replaces them with the real team ids.
    _COMPLETED = {"FINALIZADO", "SUSPENDIDO", "CANCELADO"}
    teams_in_played_games: set[str] = set()
    for g in games:
        if g.get("status") not in _COMPLETED:
            continue
        if g.get("home_team_id"):
            teams_in_played_games.add(g["home_team_id"])
        if g.get("away_team_id"):
            teams_in_played_games.add(g["away_team_id"])
    teams_out = [
        {
            "id": tid,
            "name": name,
            "logo_filename": team_id_to_logo.get(tid),
        }
        for tid, name in sorted(teams_by_id.items(), key=lambda x: x[1])
        if not (_is_placeholder(name) and tid not in teams_in_played_games)
    ]
    clubs = _group_clubs(teams_out)
    # Annotate each team with its club_id.
    club_id_by_team: dict[str, str] = {}
    for c in clubs:
        for tid in c["team_ids"]:
            club_id_by_team[tid] = c["id"]
    for t in teams_out:
        t["club_id"] = club_id_by_team.get(t["id"])

    players_list = [
        {
            "id": info["id"],
            "team_id": info["team_id"],
            "team_name": info["team_name"],
            "name": _best_display_name(info["name_variants"], preferred=info.get("name")),
            "name_variants": sorted(info["name_variants"]),
            "dorsals": sorted(info["dorsals"], key=lambda d: (len(d), d)),
        }
        for info in sorted(players.values(), key=lambda i: (i["team_name"] or "", i["name"]))
    ]
    club_season = _aggregate_club_season_stats(clubs, team_season, players_list, games)

    # ---- B12: head-to-head + B13: quarter leaders ----
    h2h = _head_to_head(games)
    quarter_leaders = _quarter_leaders(player_game_stats, players)

    # ---- Phase 5: referees & coaches season tables ----
    referees, referee_season = _aggregate_referees(games, log_events, player_game_stats)
    coaches, coach_season = _aggregate_coaches(games)

    # ---- competition_type, parent_category_id, phase, bracket ----
    group_obj = dict(group_meta["group"])
    comp_type = _infer_competition_type(group_obj.get("group_name") or group_obj.get("category_name"))
    group_obj["competition_type"] = comp_type
    if comp_type != "regular" and group_obj.get("category_id"):
        group_obj["parent_category_id"] = group_obj["category_id"]
    if comp_type == "final":
        phase_map = _infer_phase(games)
        for g in games:
            gid = g.get("id")
            if gid and gid in phase_map:
                g["phase"] = phase_map[gid]
        bracket = _infer_bracket(games)
        if bracket is not None:
            group_obj["bracket"] = bracket

    # ---- final output ----
    out: dict = {
        "group": group_obj,
        "teams": teams_out,
        "players": players_list,
        "clubs": clubs,
        "games": games,
        "player_game_stats": player_game_stats,
        "log_events": log_events,
        "player_season_stats": player_season,
        "team_season_stats": team_season,
        "club_season_stats": club_season,
        "head_to_head": h2h,
        "quarter_leaders": quarter_leaders,
    }
    if referees:
        out["referees"] = referees
    if referee_season:
        out["referee_season_stats"] = referee_season
    if coaches:
        out["coaches"] = coaches
    if coach_season:
        out["coach_season_stats"] = coach_season
    return out


_COMPETITION_TYPE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^\s*F4\b", re.IGNORECASE), "f4"),
    (re.compile(r"^\s*FINAL\s+COPA\b", re.IGNORECASE), "final_copa"),
    (re.compile(r"^\s*FINAL\s+TITULO\s+LIGA\b", re.IGNORECASE), "final"),
    (re.compile(r"^\s*CRUCES\b", re.IGNORECASE), "cruces"),
    (re.compile(r"^\s*PLAY[\s\-]*IN\b", re.IGNORECASE), "play_in"),
    (re.compile(r"\b2A?\s*FASE\b|TITULO\s+DE\s+LIGA\b", re.IGNORECASE), "second_phase"),
]


_PLACEHOLDER_NAME_RE = re.compile(
    r"^\s*("
    r"(GANADOR|PERDEDOR|VENCEDOR|EQUIPO)(\s*[A-Z0-9]+)?"
    r"|\d+[ºª]?\s+GRUPO\s+[A-Z0-9]+"
    r")\s*$",
    re.IGNORECASE,
)


def _is_placeholder(name: str) -> bool:
    return bool(_PLACEHOLDER_NAME_RE.match(name))


def _infer_competition_type(group_name: str | None) -> str:
    if not group_name:
        return "regular"
    for pattern, value in _COMPETITION_TYPE_PATTERNS:
        if pattern.search(group_name):
            return value
    return "regular"


def _infer_phase(games: list[dict]) -> dict[str, str | None]:
    """Infer phase label per game for a `competition_type == "final"` group.

    Maps by count (games sorted chronologically):
      1 -> [final]
      2 -> [semifinal, semifinal]   (ida y vuelta)
      3 -> [semifinal, semifinal, final]
      4 -> [semifinal, semifinal, tercer-puesto, final]
    """
    def _sort_key(g: dict) -> tuple:
        d = g.get("date")
        return (d is None, d or "", g.get("jornada") or 0)

    ordered = sorted(games, key=_sort_key)
    n = len(ordered)
    if n == 0:
        return {}
    if n == 1:
        labels: list[str | None] = ["final"]
    elif n == 2:
        labels = ["semifinal", "semifinal"]
    elif n == 3:
        labels = ["semifinal", "semifinal", "final"]
    elif n == 4:
        labels = ["semifinal", "semifinal", "tercer-puesto", "final"]
    else:
        labels = [None] * n
    result: dict[str, str | None] = {}
    for g, label in zip(ordered, labels):
        gid = g.get("id")
        if gid:
            result[gid] = label
    return result


def _infer_bracket(games: list[dict]) -> dict | None:
    """Build a bracket descriptor for a `competition_type == "final"` group.

    Returns a dict with keys "semifinals", "final", "third_place" — or None
    for a single-game direct final.
    """
    def _sort_key(g: dict) -> tuple:
        d = g.get("date")
        return (d is None, d or "", g.get("jornada") or 0)

    ordered = sorted(games, key=_sort_key)
    n = len(ordered)
    if n < 2:
        return None

    def _winner_loser(g: dict) -> tuple[str | None, str | None]:
        w = g.get("winner")
        h, a = g.get("home_team_id"), g.get("away_team_id")
        if w == "home":
            return h, a
        if w == "away":
            return a, h
        return None, None

    semis_games: list[dict] = []
    final_game: dict | None = None
    third_game: dict | None = None

    if n == 2:
        semis_games = ordered[:2]
    elif n == 3:
        semis_games = ordered[:2]
        final_game = ordered[2]
    elif n == 4:
        semis_games = ordered[:2]
        third_game = ordered[2]
        final_game = ordered[3]
    else:
        semis_games = ordered[:2]

    semis_out: list[dict] = []
    for sg in semis_games:
        winner, loser = _winner_loser(sg)
        semis_out.append({
            "game_id": sg.get("id"),
            "home_team_id": sg.get("home_team_id"),
            "away_team_id": sg.get("away_team_id"),
            "winner_team_id": winner,
            "loser_team_id": loser,
        })

    sf_winners = [s["winner_team_id"] for s in semis_out]
    sf_losers = [s["loser_team_id"] for s in semis_out]

    def _slot(game: dict | None, expected_a: str | None, expected_b: str | None) -> dict:
        if game is None:
            return {
                "game_id": None,
                "expected_home_team_id": expected_a,
                "expected_away_team_id": expected_b,
                "winner_team_id": None,
            }
        winner, _ = _winner_loser(game)
        return {
            "game_id": game.get("id"),
            "expected_home_team_id": expected_a,
            "expected_away_team_id": expected_b,
            "winner_team_id": winner,
        }

    out: dict = {"semifinals": semis_out}
    if n >= 3:
        out["final"] = _slot(
            final_game,
            sf_winners[0] if len(sf_winners) > 0 else None,
            sf_winners[1] if len(sf_winners) > 1 else None,
        )
    if n >= 4:
        out["third_place"] = _slot(
            third_game,
            sf_losers[0] if len(sf_losers) > 0 else None,
            sf_losers[1] if len(sf_losers) > 1 else None,
        )
    return out


def _winner(home: int | None, away: int | None, status: str | None) -> str | None:
    if status != "FINALIZADO" or home is None or away is None:
        return None
    if home > away:
        return "home"
    if away > home:
        return "away"
    return "draw"


def _clock_to_seconds(clock: str | None) -> int | None:
    """Parse a countdown clock string 'MM:SS' into total seconds."""
    if not clock:
        return None
    parts = clock.split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


def _derive_plus_minus_per_game(log_raw: list[dict], side_to_player_pgs: dict[str, list[dict]]) -> None:
    """Compute period-level plus/minus and estimated minutes for each player_game_stats row.

    Modifies each row in side_to_player_pgs in place, adding:
        plus_minus      (int or None for DNP)
        plus_minus_est  (True always — this is a heuristic estimate)

    Algorithm:
    - plus_minus: for each player, find all periods where they appear in at
      least one log event. Count pts scored by each side in those periods.
      plus_minus = (own_side_pts - other_side_pts).
    """
    SCORING_KINDS = {"made_2": 2, "made_3": 3, "ft_made": 1}

    # Build per-period per-side scoring totals.
    # period -> {"home": pts, "away": pts}
    period_pts: dict[str, dict[str, int]] = {}
    for e in log_raw:
        period = e.get("period")
        if not period:
            continue
        kind = e.get("event_kind", "")
        pts = SCORING_KINDS.get(kind, 0)
        if pts:
            side = e.get("side")
            if side not in ("home", "away"):
                continue
            if period not in period_pts:
                period_pts[period] = {"home": 0, "away": 0}
            period_pts[period][side] += pts

    # Collect log events per player: player_id -> period -> list of clock_seconds
    player_period_clocks: dict[str, dict[str, list[int]]] = {}

    # Build reverse maps: (side, dorsal) -> player_id and (side, name_key) -> player_id
    side_dorsal_to_pid: dict[tuple[str, str], str] = {}
    side_namekey_to_pid: dict[tuple[str, str], str] = {}
    for side_label, pgs_list in side_to_player_pgs.items():
        for row in pgs_list:
            dorsal = row.get("dorsal")
            if dorsal:
                side_dorsal_to_pid[(side_label, str(dorsal))] = row["player_id"]
            side_namekey_to_pid[(side_label, _name_key(row.get("name", "")))] = row["player_id"]

    for e in log_raw:
        pname = e.get("player_name")
        pdorsal = e.get("player_dorsal")
        side = e.get("side")
        period = e.get("period")
        if not pname or not side or not period or side not in ("home", "away"):
            continue
        # Resolve to player_id — try dorsal first, then name_key
        pid = None
        if pdorsal:
            pid = side_dorsal_to_pid.get((side, str(pdorsal)))
        if pid is None:
            pid = side_namekey_to_pid.get((side, _name_key(pname)))
        if pid is None:
            continue
        clock_secs = _clock_to_seconds(e.get("clock"))
        if pid not in player_period_clocks:
            player_period_clocks[pid] = {}
        if period not in player_period_clocks[pid]:
            player_period_clocks[pid][period] = []
        if clock_secs is not None:
            player_period_clocks[pid][period].append(clock_secs)

    # Assign plus_minus and minutes_est to each pgs row.
    for side_label, pgs_list in side_to_player_pgs.items():
        other_side = "away" if side_label == "home" else "home"
        for row in pgs_list:
            pid = row["player_id"]
            periods_active = player_period_clocks.get(pid, {})

            if not periods_active:
                # DNP — no events found
                row["plus_minus"] = None
                row["plus_minus_est"] = True
                continue

            # Plus/minus: sum scoring over all periods where player was active
            own_pts = 0
            opp_pts = 0
            for period in periods_active:
                pp = period_pts.get(period, {"home": 0, "away": 0})
                own_pts += pp.get(side_label, 0)
                opp_pts += pp.get(other_side, 0)
            row["plus_minus"] = own_pts - opp_pts
            row["plus_minus_est"] = True


def _clutch_pts_by_player(log_events: list[dict]) -> dict[str, int]:
    """Return mapping player_id -> total clutch points.

    Clutch = scoring events (made_2, made_3, ft_made) in period P4 or any
    overtime period (E1, E2, …) where clock <= 05:00 (i.e. <= 300 seconds
    remaining on a countdown clock).
    """
    def _is_clutch_period(period: str | None) -> bool:
        if not period:
            return False
        return period == "P4" or period.startswith("E")

    def _clock_seconds(clock: str | None) -> int | None:
        if not clock:
            return None
        parts = clock.split(":")
        if len(parts) != 2:
            return None
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            return None

    POINT_VALUES = {"made_2": 2, "made_3": 3, "ft_made": 1}
    result: dict[str, int] = {}
    for e in log_events:
        if e.get("player_id") is None:
            continue
        if not _is_clutch_period(e.get("period")):
            continue
        secs = _clock_seconds(e.get("clock"))
        if secs is None or secs > 300:
            continue
        pts = POINT_VALUES.get(e.get("event_kind", ""), 0)
        if pts:
            result[e["player_id"]] = result.get(e["player_id"], 0) + pts
    return result


def _player_season_stats(pgs_rows: list[dict], log_events: list[dict] | None = None,
                         game_winner_map: dict[str, str] | None = None) -> list[dict]:
    """game_winner_map: {game_id -> "home"|"away"|"draw"|None} for foul-out impact."""
    bucket: dict[str, dict] = {}
    # Phase 4 — accumulate dnp/start counters across every row (including rows
    # where the player didn't actually play this game).
    # Use sets of (player_id, game_id) to avoid double-counting players who
    # appear with two different dorsals in the same match (duplicate box-score rows).
    dnp_counts: dict[str, int] = defaultdict(int)
    start_counts: dict[str, int] = defaultdict(int)
    _dnp_seen: set[tuple[str, str]] = set()
    _start_seen: set[tuple[str, str]] = set()
    for r in pgs_rows:
        key = (r["player_id"], r["game_id"])
        if r.get("dnp") and key not in _dnp_seen:
            dnp_counts[r["player_id"]] += 1
            _dnp_seen.add(key)
        if r.get("starter") and key not in _start_seen:
            start_counts[r["player_id"]] += 1
            _start_seen.add(key)
        if not r["played"]:
            continue
        key = r["player_id"]
        if key not in bucket:
            bucket[key] = {
                "player_id": r["player_id"],
                "team_id": r["team_id"],
                "games_played": 0,
                "totals": {
                    "pts": 0, "t2": 0, "t3": 0,
                    "tl_made": 0, "tl_att": 0, "fp": 0,
                    "fp_personal": 0, "fp_technical": 0,
                    "fp_unsportsmanlike": 0, "fp_disqualifying": 0,
                    "fp_received": 0,
                },
                "per_quarter_totals": defaultdict(
                    lambda: {"pts": 0, "t2": 0, "t3": 0, "tl_made": 0, "tl_att": 0, "fp": 0}
                ),
                "highs": {"pts": 0, "t3": 0, "t2": 0, "tl_made": 0, "fp": 0, "fp_received": 0},
                "fouled_out_games": 0,
                "plus_minus_total": 0,
                # home/away splits
                "home_games": 0, "home_pts": 0,
                "home_t2": 0, "home_t3": 0, "home_tl_made": 0, "home_tl_att": 0,
                "away_games": 0, "away_pts": 0,
                "away_t2": 0, "away_t3": 0, "away_tl_made": 0, "away_tl_att": 0,
                # per-game pts list for CV
                "pts_per_game": [],
                # fouled-out game outcomes
                "fouled_out_team_wins": 0,
                "fouled_out_team_losses": 0,
            }
        b = bucket[key]
        b["games_played"] += 1
        for k in b["totals"]:
            b["totals"][k] += r.get(k, 0)
        for k in b["highs"]:
            if r.get(k, 0) > b["highs"][k]:
                b["highs"][k] = r.get(k, 0)
        b["pts_per_game"].append(r.get("pts", 0))
        # home/away splits
        side = r.get("side")
        if side == "home":
            b["home_games"] += 1
            b["home_pts"] += r.get("pts", 0)
            b["home_t2"] += r.get("t2", 0)
            b["home_t3"] += r.get("t3", 0)
            b["home_tl_made"] += r.get("tl_made", 0)
            b["home_tl_att"] += r.get("tl_att", 0)
        elif side == "away":
            b["away_games"] += 1
            b["away_pts"] += r.get("pts", 0)
            b["away_t2"] += r.get("t2", 0)
            b["away_t3"] += r.get("t3", 0)
            b["away_tl_made"] += r.get("tl_made", 0)
            b["away_tl_att"] += r.get("tl_att", 0)
        if r.get("fouled_out"):
            b["fouled_out_games"] += 1
            # track if team won or lost when this player fouled out
            if game_winner_map:
                winner = game_winner_map.get(r.get("game_id", ""))
                if winner == side:
                    b["fouled_out_team_wins"] += 1
                elif winner is not None and winner != "draw":
                    b["fouled_out_team_losses"] += 1
        for period, qs in r["by_quarter"].items():
            for k, v in qs.items():
                b["per_quarter_totals"][period][k] += v
        if r.get("plus_minus") is not None:
            b["plus_minus_total"] += r["plus_minus"]

    # B9: per-team pts total (played rows only) for pts_share computation.
    team_pts_total: dict[str, int] = defaultdict(int)
    for r in pgs_rows:
        if r.get("played") and r.get("team_id"):
            team_pts_total[r["team_id"]] += r.get("pts", 0)

    # B14: clutch pts per player from log_events.
    clutch_map: dict[str, int] = {}
    if log_events:
        clutch_map = _clutch_pts_by_player(log_events)

    out: list[dict] = []
    for b in bucket.values():
        gp = b["games_played"]
        # Backward-compat: expose combined "fp_anti" (= unsportsmanlike +
        # disqualifying) on season totals/averages so the existing web/build.py
        # leaderboard keeps working. The per-game rows no longer carry this key.
        b["totals"]["fp_anti"] = (
            b["totals"]["fp_unsportsmanlike"] + b["totals"]["fp_disqualifying"]
        )
        t2 = b["totals"]["t2"]
        t3 = b["totals"]["t3"]
        tl_att = b["totals"]["tl_att"]
        pts = b["totals"]["pts"]
        fg_att = t2 + t3
        efg_pct = _round((t2 + 1.5 * t3) / fg_att, 3) if fg_att > 0 else None
        ts_denom = 2 * (fg_att + 0.44 * tl_att)
        ts_pct = _round(pts / ts_denom, 3) if ts_denom > 0 else None
        team_total = team_pts_total.get(b["team_id"], 0)
        pts_share = round(pts / team_total, 3) if team_total > 0 else None

        # home/away splits
        hg = b["home_games"]
        ag = b["away_games"]
        home_ppg = _round(b["home_pts"] / hg) if hg else None
        away_ppg = _round(b["away_pts"] / ag) if ag else None
        home_fg = b["home_t2"] + b["home_t3"]
        away_fg = b["away_t2"] + b["away_t3"]
        home_efg = _round((b["home_t2"] + 1.5 * b["home_t3"]) / home_fg, 3) if home_fg > 0 else None
        away_efg = _round((b["away_t2"] + 1.5 * b["away_t3"]) / away_fg, 3) if away_fg > 0 else None
        delta = _round(home_ppg - away_ppg) if home_ppg is not None and away_ppg is not None else None

        # Scoring consistency: CV = stdev/mean, requires ≥5 games
        pts_list = b["pts_per_game"]
        pts_cv: float | None = None
        consistency_tier: str | None = None
        if len(pts_list) >= 5 and pts > 0:
            mean_pts = pts / gp
            variance = sum((x - mean_pts) ** 2 for x in pts_list) / len(pts_list)
            pts_cv = _round(variance ** 0.5 / mean_pts, 3) if mean_pts > 0 else None
            if pts_cv is not None:
                if pts_cv < 0.4:
                    consistency_tier = "alto"
                elif pts_cv <= 0.8:
                    consistency_tier = "medio"
                else:
                    consistency_tier = "bajo"

        # Q4 free-throw pressure (min 10 FT attempts in Q4 across season)
        q4_tl_made = b["per_quarter_totals"].get("P4", {}).get("tl_made", 0)
        q4_tl_att = b["per_quarter_totals"].get("P4", {}).get("tl_att", 0)
        # sum OT quarters for q4+ (E1, E2, ...)
        for p, qs in b["per_quarter_totals"].items():
            if p.startswith("E"):
                q4_tl_made += qs.get("tl_made", 0)
                q4_tl_att += qs.get("tl_att", 0)
        early_tl_made = b["totals"]["tl_made"] - q4_tl_made
        early_tl_att = b["totals"]["tl_att"] - q4_tl_att
        q4_ft_pct: float | None = None
        early_ft_pct: float | None = None
        clutch_ft_delta: float | None = None
        if q4_tl_att >= 10:
            q4_ft_pct = _round(q4_tl_made / q4_tl_att, 3)
        if early_tl_att > 0:
            early_ft_pct = _round(early_tl_made / early_tl_att, 3)
        if q4_ft_pct is not None and early_ft_pct is not None:
            clutch_ft_delta = _round(q4_ft_pct - early_ft_pct, 3)

        out.append({
            "player_id": b["player_id"],
            "team_id": b["team_id"],
            "games_played": gp,
            "totals": b["totals"],
            "averages": {k: _round(v / gp) for k, v in b["totals"].items()},
            "ft_pct": _round(b["totals"]["tl_made"] / b["totals"]["tl_att"], 3) if b["totals"]["tl_att"] else None,
            "efg_pct": efg_pct,
            "ts_pct": ts_pct,
            "pts_share": pts_share,
            "clutch_pts": clutch_map.get(b["player_id"], 0),
            "highs": b["highs"],
            "fouled_out_games": b["fouled_out_games"],
            "fouled_out_team_wins": b["fouled_out_team_wins"],
            "fouled_out_team_losses": b["fouled_out_team_losses"],
            "dnp_games": dnp_counts.get(b["player_id"], 0),
            "starts": start_counts.get(b["player_id"], 0),
            "plus_minus_total": b["plus_minus_total"],
            "per_quarter_totals": {p: dict(q) for p, q in sorted(b["per_quarter_totals"].items())},
            "per_quarter_averages": {
                p: {k: _round(v / gp) for k, v in q.items()}
                for p, q in sorted(b["per_quarter_totals"].items())
            },
            # home/away splits
            "home_games": hg,
            "home_ppg": home_ppg,
            "home_efg": home_efg,
            "away_games": ag,
            "away_ppg": away_ppg,
            "away_efg": away_efg,
            "home_away_delta": delta,
            # scoring consistency
            "pts_cv": pts_cv,
            "consistency_tier": consistency_tier,
            # clutch FT
            "q4_ft_pct": q4_ft_pct,
            "early_ft_pct": early_ft_pct,
            "clutch_ft_delta": clutch_ft_delta,
        })
    out.sort(key=lambda r: (-r["totals"]["pts"], -r["games_played"]))
    return out


def _team_season_stats(games: list[dict],
                       pgs_rows: list[dict] | None = None,
                       log_events: list[dict] | None = None) -> list[dict]:
    bucket: dict[str, dict] = {}
    # Sort chronologically by jornada+date for streak computation (B11).
    finalizado = [
        g for g in games
        if g["status"] == "FINALIZADO"
        and g["home_score"] is not None
        and g["away_score"] is not None
    ]
    # Sort: jornada asc, date asc (undated games go last via "9999" sentinel),
    # id as deterministic tiebreaker across runs.
    finalizado.sort(key=lambda g: (
        g.get("jornada") or 0,
        g.get("date") or "9999",
        g.get("id") or "",
    ))

    # Pre-compute bench/starter pts per (game_id, side) from player_game_stats
    bench_pts_map: dict[tuple[str, str], int] = defaultdict(int)
    starter_pts_map: dict[tuple[str, str], int] = defaultdict(int)
    fouls_by_period_map: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )  # {team_id: {period: count}}
    foulout_games: dict[str, set[str]] = defaultdict(set)  # {team_id: {game_id}} — games with ≥1 foul-out
    for r in (pgs_rows or []):
        if not r.get("played"):
            continue
        gid = r.get("game_id", "")
        side = r.get("side", "")
        tid = r.get("team_id", "")
        pts = r.get("pts", 0)
        if r.get("starter"):
            starter_pts_map[(gid, side)] += pts
        else:
            bench_pts_map[(gid, side)] += pts
        if r.get("fouled_out"):
            foulout_games[tid].add(gid)
        # fouls by period
        for period, qs in r.get("by_quarter", {}).items():
            fouls_by_period_map[tid][period]["fp"] += qs.get("fp", 0)

    # Build foulout game winner lookup
    game_winner_by_id: dict[str, str | None] = {g["id"]: g.get("winner") for g in finalizado if g.get("id")}

    for g in finalizado:
        for side, tid, scored, against in [
            ("home", g["home_team_id"], g["home_score"], g["away_score"]),
            ("away", g["away_team_id"], g["away_score"], g["home_score"]),
        ]:
            if not tid:
                continue
            if tid not in bucket:
                bucket[tid] = {
                    "team_id": tid,
                    "games_played": 0, "wins": 0, "losses": 0, "draws": 0,
                    "points_for": 0, "points_against": 0,
                    "per_quarter_for": defaultdict(int),
                    "per_quarter_against": defaultdict(int),
                    "per_quarter_games": defaultdict(int),
                    # B10: per-period W-L-T from quarters
                    "quarters_won": 0, "quarters_lost": 0, "quarters_tied": 0,
                    # B11: result history in chronological order (W/L/D)
                    "result_history": [],
                    # B5: aggregate timeouts used per team
                    "timeouts_used": 0,
                    "timeouts_games": 0,
                    # new: bench/starter
                    "bench_pts_total": 0, "starter_pts_total": 0, "bench_starter_games": 0,
                    # new: Q1 win tracking
                    "q1_wins": 0, "q1_games": 0, "q1_win_led_to_game_win": 0,
                    # new: choke/comeback (leads at end of Q3)
                    "choke_count": 0, "comeback_count": 0,
                    # new: foulout record
                    "foulout_wins": 0, "foulout_losses": 0,
                }
            b = bucket[tid]
            b["games_played"] += 1
            won_game = scored > against
            lost_game = scored < against
            if won_game:
                b["wins"] += 1
                b["result_history"].append("W")
            elif lost_game:
                b["losses"] += 1
                b["result_history"].append("L")
            else:
                b["draws"] += 1
                b["result_history"].append("D")
            b["points_for"] += scored
            b["points_against"] += against
            # B5: per-team timeouts (when present)
            t_used = g.get("timeouts_home" if side == "home" else "timeouts_away")
            if t_used is not None:
                b["timeouts_used"] += t_used
                b["timeouts_games"] += 1
            quarters = g.get("quarters") or []
            for i, q in enumerate(quarters, start=1):
                period = f"P{i}"
                qh, qa = q
                my_pts = qh if side == "home" else qa
                opp_pts = qa if side == "home" else qh
                b["per_quarter_for"][period] += my_pts
                b["per_quarter_against"][period] += opp_pts
                b["per_quarter_games"][period] += 1
                # B10: per-period win/loss/tie
                if my_pts > opp_pts:
                    b["quarters_won"] += 1
                elif my_pts < opp_pts:
                    b["quarters_lost"] += 1
                else:
                    b["quarters_tied"] += 1

            # Q1 predictor
            if quarters:
                qh0, qa0 = quarters[0]
                my_q1 = qh0 if side == "home" else qa0
                opp_q1 = qa0 if side == "home" else qh0
                if my_q1 != opp_q1:  # no ties in Q1 win tracking
                    b["q1_games"] += 1
                    if my_q1 > opp_q1:
                        b["q1_wins"] += 1
                        if won_game:
                            b["q1_win_led_to_game_win"] += 1

            # Choke / comeback (Q3 lead → final result)
            # Need at least 3 quarters
            if len(quarters) >= 3:
                cum_my = sum(
                    (qh if side == "home" else qa) for qh, qa in quarters[:3]
                )
                cum_opp = sum(
                    (qa if side == "home" else qh) for qh, qa in quarters[:3]
                )
                if cum_my > cum_opp and lost_game:
                    b["choke_count"] += 1
                elif cum_my < cum_opp and won_game:
                    b["comeback_count"] += 1

            # Bench / starter pts
            gid = g.get("id")
            if gid:
                bp = bench_pts_map.get((gid, side), 0)
                sp = starter_pts_map.get((gid, side), 0)
                if bp + sp > 0:
                    b["bench_pts_total"] += bp
                    b["starter_pts_total"] += sp
                    b["bench_starter_games"] += 1

            # Foulout record
            if gid and tid in foulout_games and gid in foulout_games[tid]:
                winner = game_winner_by_id.get(gid)
                if winner == side:
                    b["foulout_wins"] += 1
                elif winner is not None and winner != "draw":
                    b["foulout_losses"] += 1

    # Compute best seasonal scoring run from log_events
    seasonal_run_map = _best_seasonal_run(log_events or [], finalizado) if log_events else {}

    out: list[dict] = []
    for b in bucket.values():
        gp = b["games_played"]
        q_total = b["quarters_won"] + b["quarters_lost"] + b["quarters_tied"]
        # B11: streak = current run at end of history; best_streak = longest W run.
        current_streak = _current_streak(b["result_history"])
        best_streak = _best_win_streak(b["result_history"])

        # bench/starter averages
        bsg = b["bench_starter_games"]
        bench_ppg = _round(b["bench_pts_total"] / bsg) if bsg else None
        starter_ppg = _round(b["starter_pts_total"] / bsg) if bsg else None
        total_bench_starter = b["bench_pts_total"] + b["starter_pts_total"]
        bench_pct = _round(b["bench_pts_total"] / total_bench_starter, 3) if total_bench_starter else None

        # Q1
        q1_win_pct = _round(b["q1_wins"] / b["q1_games"], 3) if b["q1_games"] else None
        q1_to_game_conv = (
            _round(b["q1_win_led_to_game_win"] / b["q1_wins"], 3) if b["q1_wins"] else None
        )

        # Q4 net rating
        p4_for = b["per_quarter_for"].get("P4", 0)
        p4_against = b["per_quarter_against"].get("P4", 0)
        p4_games = b["per_quarter_games"].get("P4", 0)
        q4_net_rating = (
            _round((p4_for - p4_against) / p4_games) if p4_games else None
        )

        # fouls by period
        tid = b["team_id"]
        fouls_by_period = {
            p: {"fp": v["fp"]}
            for p, v in sorted(fouls_by_period_map.get(tid, {}).items())
        } or None

        # foulout record
        fo_total = b["foulout_wins"] + b["foulout_losses"]
        record_when_foulout = (
            {"wins": b["foulout_wins"], "losses": b["foulout_losses"]}
            if fo_total > 0 else None
        )

        out.append({
            "team_id": tid,
            "games_played": gp,
            "wins": b["wins"], "losses": b["losses"], "draws": b["draws"],
            "win_pct": _round(b["wins"] / gp, 3) if gp else 0,
            "points_for": b["points_for"],
            "points_against": b["points_against"],
            "point_diff": b["points_for"] - b["points_against"],
            "avg_points_for": _round(b["points_for"] / gp) if gp else 0,
            "avg_points_against": _round(b["points_against"] / gp) if gp else 0,
            "quarters_won": b["quarters_won"],
            "quarters_lost": b["quarters_lost"],
            "quarters_tied": b["quarters_tied"],
            "quarters_win_pct": _round(b["quarters_won"] / q_total, 3) if q_total else None,
            "current_streak": current_streak,
            "best_win_streak": best_streak,
            "result_history": list(b["result_history"]),
            "timeouts_used": b["timeouts_used"],
            "timeouts_avg": _round(b["timeouts_used"] / b["timeouts_games"]) if b["timeouts_games"] else None,
            "per_quarter": {
                period: {
                    "points_for": b["per_quarter_for"][period],
                    "points_against": b["per_quarter_against"][period],
                    "avg_for": _round(b["per_quarter_for"][period] / b["per_quarter_games"][period]),
                    "avg_against": _round(b["per_quarter_against"][period] / b["per_quarter_games"][period]),
                }
                for period in sorted(b["per_quarter_for"])
            },
            # new fields
            "bench_ppg": bench_ppg,
            "bench_pct": bench_pct,
            "starter_ppg": starter_ppg,
            "q1_win_pct": q1_win_pct,
            "q1_to_game_conv": q1_to_game_conv,
            "q4_net_rating": q4_net_rating,
            "choke_count": b["choke_count"],
            "comeback_count": b["comeback_count"],
            "fouls_by_period": fouls_by_period,
            "record_when_foulout": record_when_foulout,
            "best_seasonal_run": seasonal_run_map.get(tid),
        })
    # FIBA tiebreaker (FVB-ESF Reglamento General):
    # Groups of teams tied on win_pct are broken by a sub-classification built
    # from only the games played among those teams (h2h):
    #   1. h2h win_pct   (more wins in h2h games)
    #   2. h2h point_diff (PF−PC in h2h games)
    #   3. h2h points_for (raw PF in h2h games)
    # If still tied after those three h2h steps, fall back to global stats:
    #   4. global point_diff
    #   5. global points_for
    # Walkover/forfeit: a team that did not appear has 0 pts scored, so its
    # h2h/global stats are naturally worse; no separate flag needed.
    out.sort(key=lambda r: (-r["win_pct"], -r["point_diff"]))
    out = _apply_fiba_tiebreaker(out, finalizado)
    return out


def _h2h_stats(team_ids: set[str], games: list[dict]) -> dict[str, dict]:
    """Return per-team h2h stats considering only games among `team_ids`."""
    stats: dict[str, dict] = {
        tid: {"wins": 0, "gp": 0, "pf": 0, "pa": 0} for tid in team_ids
    }
    for g in games:
        h, a = g["home_team_id"], g["away_team_id"]
        if h not in team_ids or a not in team_ids:
            continue
        hs, as_ = g["home_score"], g["away_score"]
        for tid, scored, against in [(h, hs, as_), (a, as_, hs)]:
            s = stats[tid]
            s["gp"] += 1
            s["pf"] += scored
            s["pa"] += against
            if scored > against:
                s["wins"] += 1
    return stats


def _apply_fiba_tiebreaker(rows: list[dict], games: list[dict]) -> list[dict]:
    """Re-order rows within each win_pct tie group using FIBA criteria."""
    from itertools import groupby

    result: list[dict] = []
    for _, group_iter in groupby(rows, key=lambda r: r["win_pct"]):
        group = list(group_iter)
        if len(group) == 1:
            result.extend(group)
            continue

        team_ids = {r["team_id"] for r in group}
        h2h = _h2h_stats(team_ids, games)

        def sort_key(r: dict) -> tuple:
            s = h2h[r["team_id"]]
            h2h_win_pct = s["wins"] / s["gp"] if s["gp"] else 0
            h2h_diff = s["pf"] - s["pa"]
            h2h_pf = s["pf"]
            return (
                -h2h_win_pct,
                -h2h_diff,
                -h2h_pf,
                -r["point_diff"],
                -r["points_for"],
            )

        group.sort(key=sort_key)
        result.extend(group)

    return result


def _best_scoring_run_per_game(events: list[dict]) -> tuple[dict | None, dict | None]:
    """Return (best_run_home, best_run_away) for a single game's log_events.

    A scoring run is the longest uninterrupted sequence of scoring plays by one
    side (the other side scores nothing during it). Non-scoring events are ignored.
    Returns dicts with keys: pts, period, start_seq, end_seq  (or None if no events).
    """
    scoring_kinds = {"made_2", "made_3", "ft_made"}
    scored = [e for e in events if e.get("event_kind") in scoring_kinds]
    if not scored:
        return None, None

    best: dict[str, dict | None] = {"home": None, "away": None}
    run_side: str | None = None
    run_pts = 0
    run_start_seq: int | None = None
    run_end_seq: int | None = None
    run_period: str | None = None

    def _pts(kind: str) -> int:
        if kind == "made_3":
            return 3
        if kind == "made_2":
            return 2
        return 1

    def _update_best(side: str, pts: int, period: str | None, start: int | None, end: int | None) -> None:
        if side and pts > (best[side]["pts"] if best[side] else 0):
            best[side] = {"pts": pts, "period": period, "start_seq": start, "end_seq": end}

    for e in scored:
        side = e.get("side", "")
        if side not in ("home", "away"):
            continue
        seq = e.get("seq", 0)
        pts = _pts(e.get("event_kind", ""))
        period = e.get("period")
        if side == run_side:
            run_pts += pts
            run_end_seq = seq
        else:
            if run_side:
                _update_best(run_side, run_pts, run_period, run_start_seq, run_end_seq)
            run_side = side
            run_pts = pts
            run_start_seq = seq
            run_end_seq = seq
            run_period = period
    if run_side:
        _update_best(run_side, run_pts, run_period, run_start_seq, run_end_seq)

    return best.get("home"), best.get("away")


def _best_seasonal_run(log_events: list[dict], games: list[dict]) -> dict[str, dict]:
    """Return {team_id: best_seasonal_run_dict} across all games.

    best_seasonal_run: {pts, game_id, period, start_seq, end_seq}
    """
    # index events by game_id
    events_by_game: dict[str, list[dict]] = defaultdict(list)
    for e in log_events:
        gid = e.get("game_id")
        if gid:
            events_by_game[gid].append(e)

    team_best: dict[str, dict] = {}
    for g in games:
        gid = g.get("id")
        if not gid:
            continue
        home_tid = g.get("home_team_id")
        away_tid = g.get("away_team_id")
        events = events_by_game.get(gid, [])
        best_home, best_away = _best_scoring_run_per_game(events)
        for tid, best in [(home_tid, best_home), (away_tid, best_away)]:
            if not tid or not best:
                continue
            if tid not in team_best or best["pts"] > team_best[tid]["pts"]:
                team_best[tid] = {**best, "game_id": gid}
    return team_best


def _current_streak(history: list[str]) -> dict | None:
    """Return {'type': 'W'|'L'|'D', 'length': N} for the trailing run, or None."""
    if not history:
        return None
    last = history[-1]
    length = 0
    for r in reversed(history):
        if r == last:
            length += 1
        else:
            break
    return {"type": last, "length": length}


def _best_win_streak(history: list[str]) -> int:
    best = 0
    run = 0
    for r in history:
        if r == "W":
            run += 1
            if run > best:
                best = run
        else:
            run = 0
    return best


def _head_to_head(games: list[dict]) -> list[dict]:
    """B12: per-pair (team_a < team_b) aggregate W-L-D and points.
    Includes only FINALIZADO games with both team_ids and a known winner."""
    bucket: dict[tuple[str, str], dict] = {}
    for g in games:
        if g["status"] != "FINALIZADO":
            continue
        ht, at = g.get("home_team_id"), g.get("away_team_id")
        hs, as_ = g.get("home_score"), g.get("away_score")
        if not ht or not at or hs is None or as_ is None:
            continue
        # string comparison on IDs (stable): canonicalize ordering so each pair
        # produces one bucket regardless of home/away orientation.
        a, b = (ht, at) if ht < at else (at, ht)
        key = (a, b)
        if key not in bucket:
            bucket[key] = {
                "team_a": a, "team_b": b,
                "games": 0,
                "a_wins": 0, "b_wins": 0, "draws": 0,
                "a_points": 0, "b_points": 0,
                "matches": [],
            }
        rec = bucket[key]
        rec["games"] += 1
        # Map this game's scores to (a, b) orientation.
        if ht == a:
            a_pts, b_pts = hs, as_
        else:
            a_pts, b_pts = as_, hs
        rec["a_points"] += a_pts
        rec["b_points"] += b_pts
        if a_pts > b_pts:
            rec["a_wins"] += 1
        elif a_pts < b_pts:
            rec["b_wins"] += 1
        else:
            rec["draws"] += 1
        rec["matches"].append({
            "game_id": g.get("id"),
            "jornada": g.get("jornada"),
            "date": g.get("date"),
            "a_points": a_pts,
            "b_points": b_pts,
        })
    out = list(bucket.values())
    out.sort(key=lambda r: (r["team_a"], r["team_b"]))
    return out


def _quarter_leaders(player_game_stats: list[dict], players: dict, top_n: int = 5) -> list[dict]:
    """B13: top-N scorers per period (P1..P4, E1, ...) across the season.

    Each leader entry is enriched with `player_name` and `team_name` so the
    frontend can render without a separate join. The `players` dict is keyed by
    (team_id, name_key); entries expose at least `id`, `name`, `team_name`.
    """
    # Build a fast player_id -> info lookup once.
    player_info: dict[str, dict] = {}
    for info in (players or {}).values():
        pid = info.get("id")
        if pid:
            player_info[pid] = info

    totals: dict[str, dict[str, dict]] = defaultdict(dict)  # period -> player_id -> agg
    for r in player_game_stats:
        if not r["played"]:
            continue
        pid = r["player_id"]
        for period, qs in (r.get("by_quarter") or {}).items():
            pts = qs.get("pts", 0)
            if pts <= 0:
                continue
            cell = totals[period].setdefault(pid, {
                "player_id": pid,
                "team_id": r["team_id"],
                "pts": 0, "games": 0,
            })
            cell["pts"] += pts
            cell["games"] += 1
    out: list[dict] = []
    for period in sorted(totals):
        entries = list(totals[period].values())
        for e in entries:
            e["avg"] = _round(e["pts"] / e["games"]) if e["games"] else 0
            info = player_info.get(e["player_id"])
            if info:
                e["player_name"] = info.get("name")
                e["team_name"] = info.get("team_name")
            else:
                e["player_name"] = None
                e["team_name"] = None
        # Tiebreakers: more pts, higher avg, then player_id for deterministic order.
        entries.sort(key=lambda e: (-e["pts"], -e["avg"], e["player_id"]))
        out.append({
            "period": period,
            "leaders": entries[:top_n],
        })
    return out


# ---------------------------------------------------------------------------
# Phase 5: referee & coach season aggregates
# ---------------------------------------------------------------------------

def _aggregate_referees(
    games: list[dict],
    log_events: list[dict],
    player_game_stats: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Build referees[] and referee_season_stats[] from per-game officials.

    Games with `acta_pdf_confidence == "low"` are excluded from aggregates.
    The same name with the same license collapses into one record.
    """
    # Pre-compute technicals per game (any technical foul anywhere in the log).
    techs_by_game: dict[str, int] = defaultdict(int)
    for e in log_events:
        if e.get("event_kind") == "foul_technical":
            techs_by_game[e.get("game_id") or ""] += 1

    # Pre-compute fouls per (game_id, side) from player_game_stats.
    fouls_by_game: dict[str, dict[str, int]] = defaultdict(lambda: {"home": 0, "away": 0})
    for p in player_game_stats:
        gid = p.get("game_id")
        side = p.get("side")
        fp = p.get("fp") or 0
        if gid and side in ("home", "away") and fp:
            fouls_by_game[gid][side] += fp

    # ref_id -> {name, license, name_variants, games_count, games[]}
    refs: dict[str, dict] = {}
    # ref_id -> list of (game, role) tuples
    ref_games: dict[str, list[tuple[dict, str]]] = defaultdict(list)

    for g in games:
        if g.get("acta_pdf_confidence") == "low":
            continue
        officials = g.get("officials")
        if not officials:
            continue
        # Only referee roles count for the referees[] table.
        for role in ("referee_principal", "referee_auxiliar"):
            person = officials.get(role)
            if not person:
                continue
            rid = person.get("id")
            if not rid:
                continue
            name = person.get("name")
            license_ = person.get("license")
            if rid not in refs:
                refs[rid] = {
                    "id": rid,
                    "name": name,
                    "license": license_,
                    "name_variants": set(),
                    "games_count": 0,
                }
            entry = refs[rid]
            if name:
                entry["name_variants"].add(name)
            if license_ and not entry.get("license"):
                entry["license"] = license_
            entry["games_count"] += 1
            ref_games[rid].append((g, role))

    # Pick a deterministic display name per ref: prefer the variant with one
    # comma and the most characters; stable alphabetical tie-break.
    for entry in refs.values():
        variants = entry["name_variants"]
        if variants:
            entry["name"] = max(
                variants,
                key=lambda v: (v.count(",") == 1, len(v), v),
            )

    if not refs:
        return [], []

    referees_out = []
    for rid, entry in refs.items():
        referees_out.append({
            "id": rid,
            "name": entry["name"],
            "license": entry["license"],
            "games_count": entry["games_count"],
        })
    referees_out.sort(key=lambda r: (-r["games_count"], (r["name"] or "").lower()))

    # teams index for display names in games_list
    teams_by_id: dict[str, dict] = {}
    for g in games:
        for fld in ("home_team_id", "away_team_id"):
            tid = g.get(fld)
            if tid and tid not in teams_by_id:
                teams_by_id[tid] = {"id": tid}

    season_out = []
    for rid, game_role_pairs in ref_games.items():
        gms = [g for g, _ in game_role_pairs]
        games_n = len(gms)
        techs_total = sum(techs_by_game.get(g.get("id") or "", 0) for g in gms)
        pts_total = sum((g.get("home_score") or 0) + (g.get("away_score") or 0) for g in gms)
        pts_home = sum((g.get("home_score") or 0) for g in gms)
        pts_away = sum((g.get("away_score") or 0) for g in gms)
        home_wins = sum(1 for g in gms if g.get("winner") == "home")
        # Finished games only for home_win_pct denominator.
        finalised = [g for g in gms if g.get("status") == "FINALIZADO"]
        denom = len(finalised) or 0
        # Fouls
        fp_home_total = sum(fouls_by_game[g.get("id") or ""]["home"] for g in gms if g.get("id"))
        fp_away_total = sum(fouls_by_game[g.get("id") or ""]["away"] for g in gms if g.get("id"))
        fp_denom = sum(1 for g in gms if g.get("id") and fouls_by_game.get(g["id"]))

        # Per-game detail list
        games_list = []
        for g, role in sorted(game_role_pairs, key=lambda x: (x[0].get("jornada") or 0)):
            gid = g.get("id")
            gf = fouls_by_game.get(gid, {}) if gid else {}
            hfp = gf.get("home")
            afp = gf.get("away")
            games_list.append({
                "game_id": gid,
                "jornada": g.get("jornada"),
                "date": g.get("date"),
                "role": role,
                "home_team_id": g.get("home_team_id"),
                "away_team_id": g.get("away_team_id"),
                "home_score": g.get("home_score"),
                "away_score": g.get("away_score"),
                "home_fouls": hfp if hfp is not None else None,
                "away_fouls": afp if afp is not None else None,
            })

        season_out.append({
            "referee_id": rid,
            "games": games_n,
            "technicals_in_games": techs_total,
            "avg_points_total": _round(pts_total / games_n) if games_n else 0,
            "avg_home_points": _round(pts_home / games_n) if games_n else 0,
            "avg_away_points": _round(pts_away / games_n) if games_n else 0,
            "home_win_pct": _round(home_wins / denom, 3) if denom else None,
            "avg_home_fouls": _round(fp_home_total / fp_denom) if fp_denom else None,
            "avg_away_fouls": _round(fp_away_total / fp_denom) if fp_denom else None,
            "games_list": games_list,
        })
    season_out.sort(key=lambda r: (-r["games"], r["referee_id"]))
    return referees_out, season_out


def _aggregate_coaches(games: list[dict]) -> tuple[list[dict], list[dict]]:
    """Build coaches[] and coach_season_stats[] from per-game coaches.

    Games with `acta_pdf_confidence == "low"` are excluded from aggregates.
    Only the HEAD coach is counted (assistants are tracked per-game only).
    """
    # coach_id -> entry
    coaches: dict[str, dict] = {}
    # coach_id -> list of (game, side) tuples
    coach_games: dict[str, list[tuple[dict, str]]] = defaultdict(list)

    for g in games:
        if g.get("acta_pdf_confidence") == "low":
            continue
        cmap = g.get("coaches")
        if not cmap:
            continue
        for side_key in ("home", "away"):
            blob = cmap.get(side_key)
            if not blob:
                continue
            head = blob.get("head")
            if not head:
                continue
            cid = head.get("id")
            if not cid:
                continue
            name = head.get("name")
            license_ = head.get("license")
            tid = blob.get("team_id")
            if cid not in coaches:
                coaches[cid] = {
                    "id": cid,
                    "name": name,
                    "license": license_,
                    "name_variants": set(),
                    "team_ids": set(),
                    "games_count": 0,
                }
            entry = coaches[cid]
            if name:
                entry["name_variants"].add(name)
            if license_ and not entry.get("license"):
                entry["license"] = license_
            if tid:
                entry["team_ids"].add(tid)
            entry["games_count"] += 1
            coach_games[cid].append((g, side_key))

    if not coaches:
        return [], []

    for entry in coaches.values():
        variants = entry["name_variants"]
        if variants:
            entry["name"] = max(
                variants,
                key=lambda v: (v.count(",") == 1, len(v), v),
            )

    # team_ids and games_count for coaches[] are derived from FINALISED games
    # only — keeps them consistent with coach_season_stats[] downstream.
    final_team_ids: dict[str, set[str]] = defaultdict(set)
    final_games: dict[str, int] = defaultdict(int)
    for cid, pairs in coach_games.items():
        for g, side in pairs:
            if g.get("status") != "FINALIZADO":
                continue
            final_games[cid] += 1
            tid = (g.get("coaches") or {}).get(side, {}).get("team_id")
            if tid:
                final_team_ids[cid].add(tid)

    coaches_out = []
    for cid, entry in coaches.items():
        coaches_out.append({
            "id": cid,
            "name": entry["name"],
            "license": entry["license"],
            "team_ids": sorted(final_team_ids.get(cid, set())),
            "games_count": final_games.get(cid, 0),
        })
    coaches_out.sort(key=lambda c: (-c["games_count"], (c["name"] or "").lower()))

    season_out = []
    for cid, pairs in coach_games.items():
        finalised = [(g, s) for g, s in pairs if g.get("status") == "FINALIZADO"]
        gp = len(finalised)
        wins = losses = draws = 0
        pf_total = pa_total = 0
        team_ids: set[str] = set()
        for g, side in finalised:
            tid = (g.get("coaches") or {}).get(side, {}).get("team_id")
            if tid:
                team_ids.add(tid)
            my = g.get("home_score") if side == "home" else g.get("away_score")
            opp = g.get("away_score") if side == "home" else g.get("home_score")
            if my is None or opp is None:
                continue
            pf_total += my
            pa_total += opp
            if g.get("winner") == side:
                wins += 1
            elif g.get("winner") == "draw":
                draws += 1
            else:
                losses += 1
        season_out.append({
            "coach_id": cid,
            "team_ids": sorted(team_ids),
            "games": gp,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "win_pct": _round(wins / gp, 3) if gp else 0,
            "avg_points_for": _round(pf_total / gp) if gp else 0,
            "avg_points_against": _round(pa_total / gp) if gp else 0,
            "point_diff": pf_total - pa_total,
        })
    season_out.sort(key=lambda c: (-c["games"], c["coach_id"]))
    return coaches_out, season_out


# ---------------------------------------------------------------------------
# Chronicle generation (optional Groq integration)
# ---------------------------------------------------------------------------

def _generate_cronicas_for_group(
    group_dir: Path,
    db: dict,
    api_key: str,
    workers: int,
    db_out: Path,
) -> None:
    """
    Generate ES+EU chronicles for every FINALIZADO game that lacks one.
    Writes results back into each match JSON, then re-serialises database.json
    with the updated cronica_* fields folded in via build_database().
    Imported lazily so stats.py has zero extra runtime cost when --groq-api-key
    is not passed.
    """
    import sys as _sys
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    _scripts = Path(__file__).parent / "scripts"
    if str(_scripts) not in _sys.path:
        _sys.path.insert(0, str(_scripts))

    try:
        from cronica_groq import (  # type: ignore
            build_game_context,
            generate_cronica,
            should_generate,
            write_cronica_to_match,
        )
    except ImportError as e:
        log.warning("cronica_groq not available — skipping chronicle generation: %s", e)
        return

    matches_dir = group_dir / "matches"
    if not matches_dir.exists():
        return

    pending = [
        (mp, mp.stem)
        for mp in sorted(matches_dir.glob("*.json"))
        if should_generate(mp)
    ]

    if not pending:
        log.info("  chronicles: no new FINALIZADO games — nothing to generate")
        return

    log.info("  chronicles: generating for %d game(s) with %d worker(s)…", len(pending), workers)
    ok = fail = 0

    def _process(match_path: Path, game_id: str) -> tuple[str, bool, str]:
        try:
            ctx = build_game_context(db, game_id)
        except ValueError as exc:
            return game_id, False, f"context error: {exc}"
        result = generate_cronica(game_id, ctx, api_key)
        if not result.get("cronica_es"):
            return game_id, False, "no ES text returned"
        write_cronica_to_match(match_path, result["cronica_es"], result.get("cronica_eu"))
        eu = "ES+EU" if result.get("cronica_eu") else "ES only"
        return game_id, True, eu

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_process, mp, gid): gid for mp, gid in pending}
        for fut in _as_completed(futures):
            gid = futures[fut]
            try:
                _, success, msg = fut.result()
            except Exception as exc:
                log.warning("  [%s] unexpected error: %s", gid, exc)
                fail += 1
                continue
            if success:
                log.info("  [%s] ✓ %s", gid, msg)
                ok += 1
            else:
                log.warning("  [%s] ✗ %s", gid, msg)
                fail += 1

    log.info("  chronicles done — ok: %d  fail: %d", ok, fail)

    if ok:
        # Re-build database.json so cronica_* fields appear in games[]
        log.info("  Rebuilding database.json with chronicle data…")
        rebuilt = build_database(group_dir)
        db_out.write_text(json.dumps(rebuilt, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("  database.json updated.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("group_dir", type=Path, help="Path to data/<group-slug> directory")
    p.add_argument("--out", type=Path, help="Output JSON file (default: <group_dir>/database.json)")
    p.add_argument("--groq-api-key", metavar="KEY", help="Groq API key — generate ES+EU chronicles for new FINALIZADO games")
    p.add_argument("--groq-workers", type=int, default=4, metavar="N", help="Parallel Groq workers when generating chronicles (default: 4)")
    args = p.parse_args()

    out = args.out or (args.group_dir / "database.json")
    db = build_database(args.group_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")

    # Optional: generate chronicles for new FINALIZADO games via Groq
    if args.groq_api_key:
        _generate_cronicas_for_group(args.group_dir, db, args.groq_api_key, args.groq_workers, out)
    lines = [
        f"Wrote {out}",
        f"  teams: {len(db['teams'])}",
        f"  players: {len(db['players'])}",
        f"  games: {len(db['games'])}",
        f"  player_game_stats: {len(db['player_game_stats'])}",
        f"  log_events: {len(db['log_events'])}",
        f"  player_season_stats: {len(db['player_season_stats'])}",
        f"  team_season_stats: {len(db['team_season_stats'])}",
        f"  clubs: {len(db['clubs'])}",
        f"  club_season_stats: {len(db['club_season_stats'])}",
        f"  head_to_head: {len(db['head_to_head'])}",
        f"  quarter_leaders: {len(db['quarter_leaders'])}",
    ]
    if db.get("referees"):
        lines.append(f"  referees: {len(db['referees'])}")
    if db.get("referee_season_stats"):
        lines.append(f"  referee_season_stats: {len(db['referee_season_stats'])}")
    if db.get("coaches"):
        lines.append(f"  coaches: {len(db['coaches'])}")
    if db.get("coach_season_stats"):
        lines.append(f"  coach_season_stats: {len(db['coach_season_stats'])}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
