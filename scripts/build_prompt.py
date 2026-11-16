#!/usr/bin/env python3
"""
Generate the finished extraction prompt from a template + dsb_vocab.py.

    python build_prompt.py                       # prompt_template.md -> prompt_new8.md
    python build_prompt.py tmpl.md out.md        # explicit paths

The template is the prompt with its "Appendix — controlled vocabularies" section replaced
by a single marker line:

    {{VOCAB_APPENDIX}}

Everything else in the template is copied through untouched. The appendix — canonical
term lists, synonym mapping blocks, and per-field notes — is rendered from dsb_vocab.py,
which is the single source of truth also used by the extractor and the validator.

WORKFLOW: edit dsb_vocab.py, run this, and the prompt is regenerated. Never hand-edit the
appendix in the generated file; the next build would overwrite it.
"""

import os
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
import dictionary
import vocab

MARKER = "{{VOCAB_APPENDIX}}"

HEADER = """## Appendix — facets

<!-- GENERATED from scripts/vocab.py and prompt/dictionary.json by build_prompt.py.
     Do not hand-edit this section. -->

These are the closed facets. A phrase is the entry's own word and is never taken from this list.
A facet is `None` when no facet fits. Discipline facets are the Dictionary's field headings."""


def render_field(field, spec):
    """Render one field's appendix entry: intro, term groups, mapping, notes."""
    out = [spec["intro"]]

    groups = spec["groups"]
    if spec.get("inline"):
        # one flat comma-separated line (Gender, disciplines)
        terms = [t for group in groups.values() for t in group]
        out.append(" " + ", ".join(terms) + ".")
    else:
        out.append("")
        for label, terms in groups.items():
            if not terms:
                continue
            prefix = f"- *{label}:* " if label else "- "
            out.append(prefix + ", ".join(terms) + ".")

    if spec["synonyms"]:
        out.append("")
        out.append("**Mapping** — route the term the entry uses to the canonical term on "
                   "the left:")
        out.append("")
        # group variants by their target so the block reads as "Target ← a, b, c"
        by_target = {}
        for variant, target in spec["synonyms"].items():
            by_target.setdefault(target, []).append(variant)
        # keep canonical display order
        order = [t for group in groups.values() for t in group]
        for target in sorted(by_target, key=lambda t: order.index(t) if t in order else 999):
            variants = ", ".join(by_target[target])
            out.append(f"- **{target}** ← {variants}.")

    if spec["notes"]:
        out.append("")
        out.append(spec["notes"])

    return "\n".join(out)


def render_appendix():
    parts = [HEADER, ""]
    facets = dictionary.discipline_facets()
    parts.append("**Scientific_Disciplines_Facet:** " + ", ".join(facets) + ".")
    parts.append("")
    parts.append(
        "Match a header term to this list. A header term that matches none is not given "
        "a nearby facet. The body's words stay in `Scientific_Disciplines` and are not rewritten to these headings."
    )
    parts.append("")
    for field, spec in vocab.FIELDS.items():
        parts.append(render_field(field, spec))
        parts.append("")
    if vocab.DEGREE_SPELLING:
        parts.append("**Degree spelling.** These are two spellings of one diploma. Write the form on the left. Do not use this list to rename a different diploma.")
        parts.append("")
        by_target = {}
        for variant, target in vocab.DEGREE_SPELLING.items():
            by_target.setdefault(target, []).append(variant)
        for target in sorted(by_target):
            parts.append(f"- **{target}** ← {', '.join(by_target[target])}.")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def main():
    template = sys.argv[1] if len(sys.argv) > 1 else os.path.join(paths.PROMPT_DIR, "prompt_template.md")
    out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(paths.PROMPT_DIR, "prompt.md")

    problems = vocab.self_check()
    if problems:
        print("Vocabulary self-check FAILED — fix dsb_vocab.py before building:")
        for p in problems:
            print("  -", p)
        sys.exit(1)

    try:
        with open(template, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        sys.exit(f"Template not found: {template}")

    if MARKER not in text:
        sys.exit(f"Template has no {MARKER} marker — nowhere to put the appendix.")

    text = text.replace(MARKER, render_appendix())
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)

    n_terms = sum(len(vocab.canonical_set(f)) for f in vocab.FIELDS)
    n_syn = sum(len(vocab.FIELDS[f]["synonyms"]) for f in vocab.FIELDS)
    print(f"Built {out_path} from {template}")
    print(f"  {len(vocab.FIELDS)} fields | {n_terms} canonical terms | {n_syn} synonyms")
    print(f"  Point SYSTEM_PROMPT_PATH at {out_path}")


if __name__ == "__main__":
    main()