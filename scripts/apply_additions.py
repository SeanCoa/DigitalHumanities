#!/usr/bin/env python3
"""
Permanently fold dsb_vocab_additions.json into your vocabulary file, then remove the sidecar.

    python apply_additions.py

Use this every few volumes to keep the vocabulary file the complete, readable record of
the vocabulary — instead of letting additions accumulate forever in the sidecar JSON.

HOW IT WORKS (and why it's safe):
  It does NOT rewrite your authored FIELDS literal. It appends a small, clearly-dated,
  self-guarding block of plain Python just before an anchor near the end of the file — the
  block adds new canonical terms to each field's "Reviewed additions" group and updates its
  synonyms. The block skips any term already canonical and is safe to have run more than
  once, so repeated folds never duplicate. Your comments, intros, and notes are untouched.

SAFETY: writes a timestamped .bak first, then re-imports and runs self_check(). If the
check fails, it restores the backup and leaves the sidecar in place.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys


def _import_paths():
    cur = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):
        folder = os.path.join(cur, "scripts")
        if os.path.isfile(os.path.join(folder, "project_paths.py")):
            if folder not in sys.path:
                sys.path.insert(0, folder)
            import project_paths
            return project_paths
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    sys.exit("Could not find scripts/project_paths.py")


paths = _import_paths()

HERE = paths.SCRIPTS                                        # vocab.py lives here, even if this script is the copy
VOCAB_PY = os.path.join(HERE, "vocab.py")                   # your vocabulary file
MODULE = os.path.splitext(os.path.basename(VOCAB_PY))[0]    # "vocab.py" -> import vocab
SIDECAR = os.path.join(HERE, "dsb_vocab_additions.json")
# Insert the folded block just before the first anchor found. The first exists only once
# the sidecar-merge machinery is present; the second (the __main__ guard) exists in every
# version, so the fold works even on a vocabulary file that lacks auto-merge.
ANCHORS = ["_MERGED = _merge_additions()", 'if __name__ == "__main__":']


def build_block(data, stamp):
    lines = [f"\n# --- review additions folded in on {stamp} (from dsb_vocab_additions.json) ---"]
    for field, change in data.items():
        canon = change.get("canon") or []
        syn = change.get("syn") or {}
        if canon:
            lines.append(f'for _t in {json.dumps(canon, ensure_ascii=False)}:')
            lines.append(f'    _g = FIELDS[{field!r}]["groups"].setdefault("Reviewed additions", [])')
            lines.append(f'    if _t not in _g and _t not in canonical_set({field!r}):')
            lines.append(f'        _g.append(_t)')
        if syn:
            lines.append(f'FIELDS[{field!r}]["synonyms"].update({json.dumps(syn, ensure_ascii=False)})')
    return "\n".join(lines) + "\n"


def main():
    if not os.path.exists(SIDECAR):
        sys.exit(f"No dsb_vocab_additions.json at {SIDECAR} — nothing to fold in.")
    try:
        data = json.load(open(SIDECAR, encoding="utf-8"))
    except Exception as e:
        sys.exit(f"Could not read {SIDECAR}: {e}")
    data = {f: c for f, c in data.items() if (c.get("canon") or c.get("syn"))}
    if not data:
        sys.exit("The additions file has no canonical terms or synonyms.")

    if not os.path.exists(VOCAB_PY):
        sys.exit(f"Vocabulary file not found: {VOCAB_PY} "
                 f"(edit VOCAB_PY at the top of this script if it has a different name).")
    src = open(VOCAB_PY, encoding="utf-8").read()
    anchor = next((a for a in ANCHORS if a in src), None)
    if anchor is None:
        sys.exit(f"Could not find an insertion point in {os.path.basename(VOCAB_PY)} "
                 f"(looked for: {ANCHORS}).")

    stamp = datetime.date.today().isoformat()
    block = build_block(data, stamp)
    new_src = src.replace(anchor, block + "\n" + anchor, 1)

    # back up, write, validate; restore on failure
    bak = VOCAB_PY + "." + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".bak"
    shutil.copy2(VOCAB_PY, bak)
    open(VOCAB_PY, "w", encoding="utf-8").write(new_src)

    check = subprocess.run(
        [sys.executable, "-c",
         f"import importlib,{MODULE} as m; importlib.reload(m); "
         "p=m.self_check(); print('\\n'.join(p)); import sys; sys.exit(1 if p else 0)"],
        cwd=HERE, capture_output=True, text=True)
    if check.returncode != 0:
        shutil.copy2(bak, VOCAB_PY)
        print(f"Self-check FAILED after folding in — {os.path.basename(VOCAB_PY)} restored from backup.")
        print(check.stdout.strip() or check.stderr.strip())
        print(f"(backup kept at {os.path.basename(bak)}; sidecar left in place)")
        sys.exit(1)

    os.remove(SIDECAR)
    n_canon = sum(len(c.get("canon") or []) for c in data.values())
    n_syn = sum(len(c.get("syn") or {}) for c in data.values())
    print(f"Folded {n_canon} canonical term(s) and {n_syn} synonym(s) into "
          f"{os.path.basename(VOCAB_PY)} across {len(data)} field(s), before anchor: {anchor!r}")
    print(f"Removed {os.path.basename(SIDECAR)}. Backup: {os.path.basename(bak)}")
    print("Next: run build_prompt.py and export_vocab.py so the prompt and review tool catch up.")


if __name__ == "__main__":
    main()