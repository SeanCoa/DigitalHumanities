#!/usr/bin/env python3
"""
Standalone validator for a DSB output CSV. No API key, no network, no cost.

Set the file to check in the CONFIG block below, or pass it on the command line
(the command line wins if given):

    python dsb_validate.py                          # checks INPUT_CSV from CONFIG
    python dsb_validate.py path/to/some_output.csv  # checks that file instead
    python dsb_validate.py in.csv report.txt        # custom report path too

Runs the same checks as the report at the end of agreement mode, and reuses the very
same validate_row() from extract.py -- so the two can never drift apart.

If the CSV has the agreement columns (Disagreement_Count / Inconsistent_Fields), the
run-to-run disagreements are folded in as issues automatically, exactly as in agreement
mode. Without those columns you get the mechanical checks only.
"""

# ============================ CONFIG ============================
# The CSV to validate. Change this to the file you want to check, or leave it as "" to
# fall back to extract.py's OUTPUT_CSV. A command-line argument overrides this.
INPUT_CSV = "rafa.csv"
# Where to write the report. A second command-line argument overrides this.
REPORT_PATH = "dsb_validation.txt"
# Re-report run-to-run disagreements? Set False when validating a file you've already
# reviewed in review_tool.html -- you resolved the disagreements there, so this run should
# do the mechanical checks only (bad dates, out-of-range codes, unmapped synonyms,
# duplicate tokens) rather than re-listing disagreements you've handled. Set True for a
# fresh extraction, to see disagreements alongside the mechanical checks.
SHOW_DISAGREEMENTS = False
# ===============================================================

import csv
import os
import sys
import types

# extract.py imports the anthropic SDK at module level. We only need its pure functions,
# so stub the module if it isn't installed -- nothing here touches the API.
try:
    import anthropic  # noqa: F401
except ImportError:
    sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=object)

try:
    import extract as dbe
except ImportError:
    sys.exit("Could not import extract.py -- run this from the same folder.")


def parse_agreement_report(path):
    """Pull per-figure run values out of an agreement report, so the standalone validator
    can show them the way extract.py does. Returns {figure heading: [(field, [v1, v2]), ...]}.
    The report's per-entry section looks like:

        === Abbe, Ernst ===
          Scientific_Disciplines:
             run1: Physics; Optics
             run2: Physics; Astronomy
    """
    out, figure, field = {}, None, None
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("===") and stripped.endswith("==="):
            figure = stripped.strip("= ").strip()
            out.setdefault(figure, [])
            field = None
        elif figure and stripped.endswith(":") and not stripped.startswith("run"):
            field = stripped[:-1]
            out[figure].append((field, []))
        elif figure and field and stripped.startswith("run"):
            _, _, value = stripped.partition(":")
            out[figure][-1][1].append(value.strip())
    return out


def main():
    # Precedence: command-line arg > CONFIG INPUT_CSV > extract.py's OUTPUT_CSV
    in_csv = sys.argv[1] if len(sys.argv) > 1 else (INPUT_CSV or dbe.OUTPUT_CSV)
    report = sys.argv[2] if len(sys.argv) > 2 else REPORT_PATH

    if not os.path.exists(in_csv):
        sys.exit(f"Input CSV not found: {in_csv}  "
                 f"(set INPUT_CSV in the CONFIG block, or pass a path on the command line.)")

    with open(in_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"{in_csv} has no data rows.")

    diffs_by_index = {}
    if not SHOW_DISAGREEMENTS:
        print("SHOW_DISAGREEMENTS is off -- mechanical checks only "
              "(disagreements were resolved in review_tool.html).")
    else:
        # If the matching agreement report is on hand, use it for the actual run values.
        agreement = parse_agreement_report(dbe.AGREEMENT_REPORT)
        by_position = list(agreement.values()) if len(agreement) == len(rows) else None
        if by_position:
            print(f"Read run values for {len(agreement)} figures from {dbe.AGREEMENT_REPORT}.")
        if by_position:
            for i, diffs in enumerate(by_position):
                if diffs:
                    diffs_by_index[i] = diffs
        elif "Inconsistent_Fields" in rows[0]:
            # fall back to field names only (no values stored in the CSV)
            for i, row in enumerate(rows):
                raw = (row.get("Inconsistent_Fields") or "").strip()
                if raw and raw != "None":
                    diffs_by_index[i] = [f.strip() for f in raw.split(";") if f.strip()]
            print(f"Found agreement columns: {len(diffs_by_index)} row(s) had a disagreement.")
            print(f"(No usable {dbe.AGREEMENT_REPORT} -- showing field names without values.)")
        else:
            print("No Inconsistent_Fields column -- running mechanical checks only.")

    dbe.VALIDATION_REPORT = report
    print(f"Validating {len(rows)} rows from {in_csv} ...")
    dbe.write_validation_report(rows, diffs_by_index)


if __name__ == "__main__":
    main()