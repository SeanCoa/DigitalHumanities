#!/usr/bin/env python3
"""
Re-check an extraction against the entry text.

    python scripts/verify.py extracted.csv raw_vol.csv evidence.csv -o gated.csv

Facts whose quotation is not in the entry are replaced with Unknown or None.
The report lists every replacement. The finished catalogs are not inputs to
this script; run it on a new extraction before review.
"""

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ground


def main():
    parser = argparse.ArgumentParser(description="Blank extracted facts the entry does not support.")
    parser.add_argument("extracted")
    parser.add_argument("raw")
    parser.add_argument("evidence")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--report", help="defaults to <output>.txt")
    args = parser.parse_args()
    report_path = args.report or os.path.splitext(args.output)[0] + "_grounding.txt"

    with open(args.raw, newline="", encoding="utf-8") as f:
        raw_rows = list(csv.DictReader(f))
    with open(args.extracted, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames)
        rows = list(reader)
    with open(args.evidence, newline="", encoding="utf-8") as f:
        evidence = list(csv.DictReader(f))
    if not (len(raw_rows) == len(rows) == len(evidence)):
        sys.exit(f"Row counts differ: raw {len(raw_rows)}, extracted {len(rows)}, evidence {len(evidence)}")

    blanked = 0
    with open(report_path, "w", encoding="utf-8") as report:
        report.write(f"GROUNDING  {args.extracted}\nRows: {len(rows)}\n\n")
        for i, (row, raw, ev) in enumerate(zip(rows, raw_rows, evidence), 1):
            content = (raw.get("Content") or "").replace("\\n", "\n")
            sources = (raw.get("Sources") or "").replace("\\n", "\n")
            heading = raw.get("Name") or ""
            issues = ground.apply(row, ev, content, sources, heading)
            name = f"{row.get('Last_Name', '?')}, {row.get('First_Name', '?')}"
            if issues:
                blanked += len(issues)
                report.write(f"Row {i} [{name}]: {len(issues)} fact(s) not kept\n")
                for issue in issues:
                    report.write(f"    - {issue}\n")
            else:
                report.write(f"Row {i} [{name}]: kept\n")
        report.write(f"\nFacts not kept: {blanked}\n")

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {args.output}")
    print(f"Wrote {report_path} ({blanked} facts not kept)")


if __name__ == "__main__":
    main()
