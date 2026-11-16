"""Locations in the current project tree.

Scripts live in scripts/. Inputs, the prompt, the archive, and the databases
are siblings of that folder, not files sitting next to the script.
"""

import os
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)

RAW_DIR = os.path.join(ROOT, "raw_volumes")
PROMPT_DIR = os.path.join(ROOT, "prompt")
ARCHIVE_DIR = os.path.join(ROOT, "archive")
DATABASE_DIR = os.path.join(ROOT, "databases")
REVIEW_DIR = os.path.join(ROOT, "review_tools")
VIS_DIR = os.path.join(ROOT, "visualizations")

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


def require_layout():
    """Exit if this checkout is missing the folders the pipeline reads and writes."""
    missing = [
        name for name, path in (
            ("raw_volumes", RAW_DIR),
            ("prompt", PROMPT_DIR),
            ("archive", ARCHIVE_DIR),
            ("databases", DATABASE_DIR),
            ("scripts/vocab.py", os.path.join(SCRIPTS, "vocab.py")),
        )
        if not os.path.exists(path)
    ]
    if missing:
        raise SystemExit(
            "Project layout is incomplete; missing: " + ", ".join(missing)
        )
