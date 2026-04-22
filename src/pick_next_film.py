#!/usr/bin/env python3
"""Vybere další film z backlogu, který ještě není nahraný.

Vstup: backlog/sledujteto-films.jsonl + state/uploaded.json
Pořadí: jak je v backlogu (nemá smysluplný priority_score — všechny 0.0).
Filtr: default jen `lang_class in {CZ_DUB, CZ_SUB}` (uživatelé chtějí český
zvuk nebo české titulky). Přepsatelné `LANG_CLASSES` env (comma-separated).
"""
import json
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BACKLOG = REPO_ROOT / "backlog" / "sledujteto-films.jsonl"
STATE = REPO_ROOT / "state" / "uploaded.json"


DEFAULT_LANG_CLASSES = {"CZ_DUB", "CZ_SUB"}


def load_backlog(path: Path = BACKLOG) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def load_state(path: Path = STATE) -> dict:
    if not path.exists():
        return {"schema_version": 1, "uploads": [], "failed_attempts": []}
    return json.loads(path.read_text())


def excluded_ids(state: dict, extra: set[int] | None = None) -> set[int]:
    """sledujteto upload_id které vynecháme: uploaded + moderated + dočasné extra."""
    done = {u["sledujteto_file_id"] for u in state.get("uploads", [])}
    skipped = {u.get("sledujteto_file_id") for u in state.get("moderated_out", [])}
    skipped.discard(None)
    return done | skipped | (extra or set())


def _lang_classes() -> set[str]:
    raw = os.environ.get("LANG_CLASSES", "").strip()
    if not raw:
        return DEFAULT_LANG_CLASSES
    if raw.upper() == "ALL":
        return set()  # no filter
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


def pick_next(
    state: dict,
    backlog_rows: list[dict],
    extra_exclude: set[int] | None = None,
) -> dict | None:
    excluded = excluded_ids(state, extra_exclude)
    allowed = _lang_classes()
    for r in backlog_rows:
        if r["sledujteto_file_id"] in excluded:
            continue
        if allowed and r.get("lang_class") not in allowed:
            continue
        return r
    return None


def display_name(film: dict) -> str:
    """Název pro zobrazení na Přehraj.to. Skladba:
        {title} ({year}) {suffix}
    kde suffix = CZ Dabing / CZ titulky / EN podle lang_class.
    """
    title = film["title"]
    year = film.get("year")
    lc = film.get("lang_class", "")
    suffix_map = {
        "CZ_DUB": "CZ Dabing",
        "CZ_SUB": "CZ titulky",
        "SK_DUB": "SK Dabing",
        "SK_SUB": "SK titulky",
        "EN": "EN",
    }
    suffix = suffix_map.get(lc, "")
    parts = [title]
    if year:
        parts.append(f"({year})")
    if suffix:
        parts.append(suffix)
    return " ".join(parts)


def main() -> int:
    if not BACKLOG.is_file():
        print(f"ERROR: backlog neexistuje: {BACKLOG}", file=sys.stderr)
        return 2

    state = load_state()
    rows = load_backlog()
    excluded = excluded_ids(state)
    print(f"[pick] state: {len(state.get('uploads', []))} hotovo, "
          f"{len(state.get('moderated_out', []))} moderated_out")
    print(f"[pick] backlog: {len(rows)} kandidátů")

    pick = pick_next(state, rows)
    if pick is None:
        print("[pick] žádný film k nahrání — backlog vyčerpán / filtrem")
        return 1

    name = display_name(pick)
    description = pick.get("description") or ""
    print(f"[pick] vybrán: sledujteto_file_id={pick['sledujteto_file_id']}, '{name}'")
    print(f"[pick] cdn={pick.get('sledujteto_cdn')}, lang={pick.get('lang_class')}")
    print(f"[pick] description ({len(description)} znaků): {description[:120]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
