#!/usr/bin/env python3
"""
Export the controlled vocabulary as vocab.json for the review tool (review_tool.html).

    python export_vocab.py            # writes vocab.json
    python export_vocab.py out.json   # custom output path

Load the resulting file via the "Vocab JSON" picker in the review tool to override its
built-in snapshot — so you review against current terms without pasting a new snapshot
into the HTML.

Run this whenever the vocabulary changes — the natural moment is right after
build_prompt.py. Because it imports dsb_vocab, it includes anything merged from
dsb_vocab_additions.json, so the export always reflects the current effective vocabulary.

The shape matches what the tool expects: {field: {"canon": [...], "syn": {variant: canonical}}},
with synonym keys lowercased (the tool looks them up case-insensitively).
"""

import json
import sys
import vocab as dsb_vocab

def build():
    out = {}
    for field in dsb_vocab.FIELDS:
        canon = sorted(dsb_vocab.canonical_set(field))
        syn = {k.lower(): v for k, v in dsb_vocab.FIELDS[field]["synonyms"].items()}
        if canon or syn:
            out[field] = {"canon": canon, "syn": syn}
    return out

def main():
    problems = dsb_vocab.self_check()
    if problems:
        print("Vocabulary self-check FAILED — fix dsb_vocab.py first:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    out = build()
    path = sys.argv[1] if len(sys.argv) > 1 else "vocab.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"wrote {path}: {len(out)} fields, "
          f"{sum(len(v['canon']) for v in out.values())} canonical terms, "
          f"{sum(len(v['syn']) for v in out.values())} synonyms")

if __name__ == "__main__":
    main()