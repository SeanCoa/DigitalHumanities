#!/usr/bin/env python3
"""
The phrase dictionary.

Societies Indexed seeds known society names. It does not say which names are
the same body, so each name starts as its own entry. List of Scientists by
Field seeds the discipline facets (the headings only). The roster of people
under those headings is never read.

Review adds rows to "accepted": a phrase, the field, the sense, a facet or
blank, and any aliases. Rebuild the seed with:

    python scripts/dictionary.py
"""

import csv
import json
import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)
import project_paths

DICTIONARY_PATH = os.path.join(project_paths.PROMPT_DIR, "dictionary.json")
SOCIETIES_CSV = os.path.join(project_paths.RAW_DIR, "societies_index.csv")
FIELDS_CSV = os.path.join(project_paths.RAW_DIR, "fields_index.csv")


def load(path=None):
    path = path or DICTIONARY_PATH
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("discipline_facets", [])
    data.setdefault("societies", [])
    data.setdefault("accepted", [])
    return data


def discipline_facets(data=None):
    data = data if data is not None else load()
    return list(data.get("discipline_facets") or [])


def society_names(data=None):
    data = data if data is not None else load()
    return [row["name"] for row in data.get("societies") or [] if row.get("name")]


def society_canonical(name, data=None):
    """The filter name for a society. The stored phrase is not rewritten."""
    data = data if data is not None else load()
    key = (name or "").strip().casefold()
    if not key:
        return name
    for row in data.get("accepted") or []:
        if row.get("field") != "Scientific_Societies_Membership":
            continue
        names = [row.get("phrase") or ""] + list(row.get("aliases") or [])
        if any(n.strip().casefold() == key for n in names if n):
            return (row.get("phrase") or name).strip()
    return name.strip()


def build_seed():
    """Read the index CSVs. People listed under a field are ignored."""
    facets = []
    seen = set()
    with open(FIELDS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            field = (row.get("field") or "").strip()
            if field and field not in seen:
                seen.add(field)
                facets.append(field)
    societies = []
    with open(SOCIETIES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            if name:
                societies.append({"name": name})
    return {
        "discipline_facets": facets,
        "societies": societies,
        "accepted": [],
    }


def save(data, path=None):
    path = path or DICTIONARY_PATH
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    seed = build_seed()
    # Keep review rows already accepted if a dictionary file exists.
    if os.path.exists(DICTIONARY_PATH):
        previous = load()
        seed["accepted"] = previous.get("accepted") or []
    save(seed)
    print(f"Wrote {DICTIONARY_PATH}")
    print(f"  {len(seed['discipline_facets'])} discipline facets")
    print(f"  {len(seed['societies'])} society names")
    print(f"  {len(seed['accepted'])} reviewed phrases kept")
