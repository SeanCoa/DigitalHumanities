#!/usr/bin/env python3
"""
DSB -> Westfall-schema batch extraction pipeline.

Reads a scraped DSB CSV (one row per figure: name, content, sources), sends each
entry to Claude through the Message Batches API (50% cheaper, async) with prompt
caching on the system prompt, parses the returned JSON, and writes a 40-column CSV
matching your database schema. Also reports any new controlled-vocabulary terms the
model introduced, so you can review and fold them into the prompt before the next run.

SETUP
  1. pip install anthropic
  2. Get an API key at console.anthropic.com and set a spending cap there.
  3. export ANTHROPIC_API_KEY="sk-ant-..."      (macOS/Linux)
     setx ANTHROPIC_API_KEY "sk-ant-..."        (Windows)
  4. Put your prompt spec and input CSV next to this file, adjust CONFIG below, run:
        python dsb_batch_extract.py

WORKFLOW
  - Start in TEST mode (MODE = "test") on a handful of entries to confirm output looks
    right and the cost is what you expect. TEST mode is synchronous (full price, instant).
  - Switch to BATCH mode (MODE = "batch") for the full run: 50% cheaper, returns within
    ~24h (usually much faster). Re-running is safe; it overwrites the output file.
  - For scaling up, VERIFY mode extracts every entry ONCE for the output, then re-checks
    only the volatile fields a second time and logs any disagreements for human review.
  - Treat the output as a FIRST PASS for human review before appending to main2.csv.
"""

import os
import csv
import sys
import json
import time
import shutil
from collections import Counter, defaultdict
import anthropic


def _import_paths():
    """Find scripts/project_paths.py from this file."""
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
import ground

# Controlled vocabularies live in ONE place: scripts/vocab.py. Both the new-term detector
# (KNOWN_VOCAB) and the validator's synonym check are derived from it, and the prompt
# appendix is generated from it by build_prompt.py -- so a new term or mapping is a
# one-line edit in one file.
try:
    import vocab as dsb_vocab
    KNOWN_VOCAB = dsb_vocab.known_vocab()
    FIELD_SYNONYM_MAPS = dsb_vocab.field_synonym_maps()
    FIELD_PARENT_MAPS = dsb_vocab.field_parent_maps()
except ImportError:      # pipeline still runs; these two checks degrade gracefully
    KNOWN_VOCAB, FIELD_SYNONYM_MAPS, FIELD_PARENT_MAPS = {}, {}, {}
    print("WARNING: scripts/vocab.py not found -- new-term detection and the synonym "
          "check are disabled.")

# ============================ CONFIG ============================
MODE = "agreement"                    # "test" (sync sample), "batch" (full run, 50% off),
                                   # "agreement" (run each entry N times, flag all disagreements),
                                   # or "verify" (single-pass output + a SECOND check on the
                                   # volatile fields only; disagreements -> agreement report)
TEST_LIMIT = 0                     # cap for test / agreement / verify mode.
                                  # Set to 0 to process the WHOLE input file (full volume).

INPUT_CSV = os.path.join(paths.RAW_DIR, "raw_vol01.csv")  # the scraped DSB file
OUTPUT_CSV = os.path.join(paths.ROOT, "dsb_extracted.csv")  # 40-column result
EVIDENCE_CSV = os.path.join(paths.ROOT, "dsb_evidence.csv")  # written only if EVIDENCE_MODE is on
NEW_TERMS_REPORT = os.path.join(paths.ROOT, "dsb_new_vocab.txt")  # new vocabulary terms for review
FAILURE_LOG = os.path.join(paths.ROOT, "dsb_failures.txt")  # raw replies that didn't parse

# --- Which volume is this? --------------------------------------------------
# Everything the run produces is copied into ARCHIVE_DIR with "_volXX" appended, e.g.
# dsb_extracted_vol01.csv -- so one volume's reports never overwrite the next one's.
# Set it here, or override at the command line:   python extract.py 3
VOLUME = 1                         # int (zero-padded to 2 digits) or a string like "01b"
ARCHIVE_DIR = paths.ARCHIVE_DIR    # archive/
ARCHIVE_PROMPT = True              # also archive the prompt used -- your method of record
SYSTEM_PROMPT_PATH = os.path.join(paths.PROMPT_DIR, "prompt.md")

# Input columns. The scraped sheet has NO header row and columns are name/content/sources.
INPUT_HAS_HEADER = True
COL_NAME, COL_CONTENT, COL_SOURCES = 0, 1, 2

MODEL = "claude-haiku-4-5"        # recommended for this task.
                                   # "claude-haiku-4-5" is ~3x cheaper - test it on a sample.
                                   # "claude-opus-4-8" is the strongest if you want max quality.
MAX_TOKENS = 10000                  # one JSON object per entry. Evidence mode roughly doubles
                                   # output, so keep this generous to avoid truncation.
EVIDENCE_MODE = True               # True -> ask for a supporting phrase per field (logged separately)
WARM_CACHE_BEFORE_BATCH = True     # one tiny sync call so the batch hits a warm prompt cache
POLL_SECONDS = 30                  # how often to check batch status

TEMPERATURE = 0                    # 0 = most deterministic. Use 0 for extraction so results
                                   # vary as little as possible between runs.
AGREEMENT_RUNS = 2                 # in "agreement" mode, how many times to extract each entry
AGREEMENT_BATCH = True            # in "agreement" mode: False = synchronous (full price, run
                                   # one entry at a time); True = submit all runs through the
                                   # Message Batches API (50% off, parallel) -- best for a
                                   # full-volume production run.
AGREEMENT_REPORT = os.path.join(paths.ROOT, "dsb_agreement.txt")  # fields that disagreed across runs
VALIDATION_REPORT = os.path.join(paths.ROOT, "dsb_validation.txt")  # mechanical checks + agreement flags

# In "verify" mode, every entry is extracted ONCE for the output row; only these volatile
# fields get a second check, and any disagreements go to AGREEMENT_REPORT.
VERIFY_FIELDS = ["Education_Locations", "Scientific_Disciplines", "Means_of_Support"]
# The verification pass. False (recommended) = a full second extraction under identical
# conditions, so a flagged disagreement reflects genuine model uncertainty. True = a cheaper
# call that asks for only the VERIFY_FIELDS -- but asking for a field in isolation changes
# the model's behavior (e.g. it stops applying cross-field rules like "study locations
# only"), which inflates and distorts the flags. The cost saving is a few cents per volume;
# not worth the noisier review queue.
VERIFY_NARROW = False

# Safety net only: if a reply ever arrives with no opening brace, the parser restores
# this so it can still recover the object. (We no longer prefill the request itself,
# because some models reject an assistant-message prefill.)
PREFILL = "{"
# ================================================================

# The 40 output columns, in your schema's order.
COLUMNS = ["Last_Name","First_Name","Alt_Name","Gender","Born_Country","Born_Town","Born_Year",
"Born_Month","Born_Day","Died_Country","Died_Town","Died_Year","Died_Month","Died_Day",
"Dates_Born_Certain","Date_Died_Certain","Dates_Lifespan","Father_Occupation",
"Father_Is_Wealthy","Father_Notes","Education_Institution","Education_Degrees",
"Education_Notes","Education_Locations","Religion_Affiliation","Religion_Affiliation_Certain","Religion_Notes",
"Scientific_Disciplines","Scientific_Disciplines_Notes","Means_of_Support",
"Means_of_Support_Notes","Patronage_Types","Patronage_Notes",
"Instruments","Instruments_Notes",
"Scientific_Societies_Membership","Scientific_Societies_Notes","Sources_List",
"Not_Available_and_Not_Consulted_List","Compiler_Details_Notes"]

# Agreement mode compares only these fields. Excluded from the comparison:
#  - "_Notes" fields: free-text paraphrase, vary in wording by design.
#  - source fields (Sources_List, Not_Available_and_Not_Consulted_List): bibliographic
#    text whose wording/condensation varies between runs.
_AGREEMENT_EXCLUDE = {"Sources_List", "Not_Available_and_Not_Consulted_List"}
COMPARED_FIELDS = [c for c in COLUMNS
                   if not c.endswith("_Notes") and c not in _AGREEMENT_EXCLUDE]

# Two extra columns agreement mode appends to the output CSV, recording how reproducible
# each figure's extraction was across runs (model self-consistency, NOT factual certainty):
#   Disagreement_Count  -- how many compared fields differed between runs
#   Inconsistent_Fields -- which fields those were ("; "-separated, "None" if fully stable)
AGREEMENT_FLAG_COLUMNS = ["Disagreement_Count", "Inconsistent_Fields"]

def load_system_prompt():
    if not os.path.exists(SYSTEM_PROMPT_PATH):
        sys.exit(f"System prompt not found at {SYSTEM_PROMPT_PATH}. "
                 "Point SYSTEM_PROMPT_PATH at the extraction-prompt spec.")
    with open(SYSTEM_PROMPT_PATH, encoding="utf-8") as f:
        prompt = f.read()
    if EVIDENCE_MODE:
        prompt += (
            "\n\n## Evidence (REQUIRED — the response is invalid without it)\n"
            "In ADDITION to every schema field, your JSON MUST include a top-level key "
            "\"_evidence\" whose value is an object. For each field whose value is not a "
            "sentinel, map the field name to a contiguous quotation of fewer than 15 words "
            "copied from CONTENT or SOURCES — the words that state the fact, not a gloss. "
            "Use \"inferred\" only for a derivation the prompt explicitly allows (gender from "
            "a pronoun in the entry, the country-name convention, or a city that is part of "
            "the institution's name). Use \"absent\" for Unknown, None, and Father_Is_Wealthy "
            "of -1. If you cannot quote the support, change the value to its sentinel and "
            "mark \"absent\". Do not omit \"_evidence\". Example: \"_evidence\": "
            "{\"Born_Year\": \"born at Basel in 1707\", \"Father_Occupation\": \"son of a pastor\"}."
        )
    return prompt


def read_entries():
    """Yield (custom_id, name, user_message) for each entry in the input CSV."""
    with open(INPUT_CSV, encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if INPUT_HAS_HEADER:
        rows = rows[1:]
    for i, r in enumerate(rows):
        if len(r) <= max(COL_NAME, COL_CONTENT, COL_SOURCES):
            continue
        name = r[COL_NAME].strip()
        content = r[COL_CONTENT].replace("\\n", "\n").strip()
        sources = r[COL_SOURCES].replace("\\n", "\n").strip() if len(r) > COL_SOURCES else ""
        if not name and not content:
            continue
        # An empty body or a title page has nothing to extract. Sending it to the
        # model produces a biography from outside knowledge (as with Brahe and Bohr
        # in volume 2) or a blank person row (the half-title page).
        if "half title" in name.lower():
            print(f"Skipping {name!r}: half-title page, not a biography.")
            continue
        if not content:
            print(f"Skipping {name!r}: entry text is empty. Nothing is extracted.")
            continue
        user_msg = f"NAME: {name}\n\nCONTENT:\n{content}\n\nSOURCES:\n{sources}"
        yield (f"entry-{i:04d}", name, user_msg)


def system_blocks(system_prompt):
    """System prompt as a cached content block (90% off on repeated input)."""
    return [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]


def build_messages(user_msg):
    """The user entry. (We don't prefill the assistant turn: some models reject it, and
    the parser already strips any stray preamble, so prefill isn't needed.)"""
    return [{"role": "user", "content": user_msg}]


def extract_text(message):
    return "".join(b.text for b in message.content if getattr(b, "type", None) == "text")


def truncation_note(stop_reason):
    """A clear hint when a reply was cut off by the token cap rather than malformed."""
    return " [response truncated - raise MAX_TOKENS]" if stop_reason == "max_tokens" else ""


def dump_failure(name, raw_text, stop_reason):
    """Append a failed reply to FAILURE_LOG so you can see exactly what came back."""
    with open(FAILURE_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n===== {name}  (stop_reason={stop_reason}, length={len(raw_text)} chars) =====\n")
        f.write(raw_text[:6000] + ("\n...[truncated in log]" if len(raw_text) > 6000 else ""))
        f.write("\n")


# USD per million tokens (input, output) -- used only for the running cost readout.
PRICING = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0, 5.0),
    "claude-opus-4-8":   (5.0, 25.0),
    "claude-opus-4-7":   (5.0, 25.0),
    "claude-opus-4-6":   (5.0, 25.0),
}


def usage_breakdown(usage):
    """(full-price input, cache-read, cache-write, output) tokens from a usage object."""
    return (getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0)


def usage_cost(usage, batched=False):
    """Estimated USD for one call. Cache reads bill at 0.1x input, writes at 1.25x;
    batch applies a further 50% discount to the whole call."""
    rate_in, rate_out = PRICING.get(MODEL, (3.0, 15.0))
    bi, cr, cw, out = usage_breakdown(usage)
    cost = (bi * rate_in + cr * rate_in * 0.1 + cw * rate_in * 1.25 + out * rate_out) / 1e6
    return cost * (0.5 if batched else 1.0)


def print_cost_summary(billed, tot_in, tot_cr, tot_cw, tot_out, tot_cost, batched=False, project_to=None):
    print(f"\n--- tokens & estimated cost{' (batch, 50% off)' if batched else ''} ---")
    print(f"  entries billed:    {billed}")
    print(f"  input full-price:  {tot_in:,}")
    print(f"  cache reads:       {tot_cr:,}   cache writes: {tot_cw:,}")
    print(f"  output:            {tot_out:,}")
    print(f"  total:             ${tot_cost:.4f}    avg/entry: ${tot_cost / max(billed, 1):.4f}")
    if project_to and billed:
        proj = tot_cost / billed * project_to
        print(f"  projected for {project_to} entries at this rate: ${proj:.2f}")
        if not batched:
            print(f"    (about ${proj * 0.5:.2f} in batch mode with the 50% discount)")


def _load_json_object(text):
    """Return the parsed JSON object from text, or None. Parses first '{' to last '}'."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _split_education(row):
    """The model emits `Education_Locations` as one list where each stop is
    'Institution @ City, Region, Country, Start_Year, End_Year'. Split it into the two aligned
    output columns, `Education_Institution` and `Education_Locations`. Because both are
    derived from the SAME list, they always have the same number of entries in the same
    order -- the model cannot desync them, which is the whole point of the one-list design.
    Any value the model may have put in `Education_Institution` is discarded and rebuilt.
    (Stops are ';'-separated; each stop's location keeps its own internal commas.)"""
    combined = (row.get("Education_Locations", "") or "").strip()
    if combined in ("", "None", "Unknown"):
        row["Education_Institution"] = combined or "None"
        row["Education_Locations"] = combined or "None"
        return
    insts, locs = [], []
    for stop in combined.split(";"):
        stop = stop.strip()
        if not stop:
            continue
        if "@" in stop:
            inst, loc = stop.split("@", 1)
            inst, loc = inst.strip(), loc.strip()
        else:
            # model omitted the institution for this stop; keep the columns aligned anyway
            inst, loc = "Not Specified", stop
        insts.append(inst or "Not Specified")
        locs.append(loc)
    row["Education_Institution"] = "; ".join(insts) if insts else "None"
    row["Education_Locations"] = "; ".join(locs) if locs else "None"


def parse_response(name, raw_text):
    """Parse the model's JSON into a row. Returns (row, evidence, error).

    Robust to every response shape. First it tries the text as-is (handles a full echoed
    object, even with stray preamble or fences, by reading first '{' to last '}'). If that
    yields nothing, it assumes the normal prefill case - where the model continued from the
    '{' we supplied and so returned no opening brace - and retries with PREFILL restored.
    """
    text = raw_text.strip()
    data = _load_json_object(text)
    if data is None and PREFILL:
        data = _load_json_object(PREFILL + text)
    if data is None:
        return None, None, "JSON parse failed: no valid JSON object in response"
    evidence = data.pop("_evidence", None)
    row = {}
    for col in COLUMNS:
        val = data.get(col, "None")               # missing key -> safe default
        row[col] = "" if val is None else str(val)
    extras = set(data) - set(COLUMNS)
    err = f"unexpected keys ignored: {sorted(extras)}" if extras else None
    _split_education(row)
    return row, evidence, err


def collect_new_terms(rows):
    """Find vocabulary terms not in KNOWN_VOCAB, grouped by field, with example figures.
    Multi-value fields are ';'-separated, so split each field on ';' to isolate its terms."""
    sentinels = {"None", "Unknown", ""}
    found = {field: {} for field in KNOWN_VOCAB}
    for row in rows:
        for field, known in KNOWN_VOCAB.items():
            for term in (t.strip() for t in row.get(field, "").split(";")):
                if term not in sentinels and term not in known:
                    found[field].setdefault(term, []).append(
                        f"{row.get('Last_Name','?')}, {row.get('First_Name','?')}")
    return {f: t for f, t in found.items() if t}


def _write_with_grounding(rows, evidence_rows, extra_columns=None):
    """Write the validation report, including facts the grounder rejected, then the CSV."""
    grounding_by_index = {}
    for i, row in enumerate(rows):
        gissues = row.pop("_grounding", None)
        if gissues:
            grounding_by_index[i] = gissues
    write_validation_report(rows, extra_by_index=grounding_by_index)
    write_outputs(rows, evidence_rows, collect_new_terms(rows), extra_columns)


def write_outputs(rows, evidence_rows, new_terms, extra_columns=None):
    fieldnames = COLUMNS + list(extra_columns or [])
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL,
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {len(rows)} rows to {OUTPUT_CSV}")

    if EVIDENCE_MODE and evidence_rows:
        keys = ["_figure"] + COLUMNS
        with open(EVIDENCE_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys, quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
            w.writeheader()
            for fig, ev in evidence_rows:
                w.writerow({"_figure": fig, **{k: str(v) for k, v in (ev or {}).items()}})
        print(f"Wrote evidence log to {EVIDENCE_CSV}")

    if new_terms:
        with open(NEW_TERMS_REPORT, "w", encoding="utf-8") as f:
            f.write("NEW VOCABULARY TERMS (not yet in the controlled lists).\n"
                    "Review: keep genuinely new terms, merge any that restate an existing one,\n"
                    "then fold the keepers into the prompt's Appendix before the next batch.\n\n")
            for field, terms in new_terms.items():
                f.write(f"## {field}\n")
                for term, figs in sorted(terms.items()):
                    f.write(f"  {term}   (e.g. {figs[0]}; {len(figs)} total)\n")
                f.write("\n")
        print(f"Flagged new vocabulary terms in {NEW_TERMS_REPORT} -- review before next run.")
    else:
        print("No new vocabulary terms.")


def run_test(client, system_prompt):
    all_entries = list(read_entries())
    entries = all_entries[:TEST_LIMIT]
    total = len(entries)
    print(f"TEST mode: {total} entries, synchronous, model={MODEL}\n")
    rows, evidence_rows, errors = [], [], []
    tot_in = tot_cr = tot_cw = tot_out = 0
    tot_cost = 0.0
    billed = 0
    for n, (cid, name, user_msg) in enumerate(entries, 1):
        try:
            msg = client.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS, temperature=TEMPERATURE,
                system=system_blocks(system_prompt),
                messages=build_messages(user_msg))
        except Exception as e:
            errors.append((name, str(e)))
            print(f"[{n}/{total}] API error  {name}: {e}")
            continue
        bi, cr, cw, out = usage_breakdown(msg.usage)
        cost = usage_cost(msg.usage, batched=False)
        tot_in += bi; tot_cr += cr; tot_cw += cw; tot_out += out
        tot_cost += cost; billed += 1
        usage_str = (f"in {bi} | cache_read {cr} | cache_write {cw} | out {out} | "
                     f"${cost:.4f} | running ${tot_cost:.4f}")
        raw = extract_text(msg)
        row, ev, perr = parse_response(name, raw)
        if row is None:
            note = truncation_note(msg.stop_reason)
            dump_failure(name, raw, msg.stop_reason)
            errors.append((name, (perr or "parse failed") + note))
            print(f"[{n}/{total}] PARSE FAIL {name}  ({usage_str}){note}")
            continue
        gissues = _ground_row(row, ev, user_msg)
        if gissues:
            row["_grounding"] = gissues
        rows.append(row)
        if ev is not None:
            evidence_rows.append((name, ev))
        flag = f"  [!] {perr}" if perr else ""
        print(f"[{n}/{total}] ok  {row['Last_Name']}, {row['First_Name']}  ({usage_str}){flag}")
    print_cost_summary(billed, tot_in, tot_cr, tot_cw, tot_out, tot_cost,
                       batched=False, project_to=len(all_entries))
    _write_with_grounding(rows, evidence_rows)
    if errors:
        print(f"\n{len(errors)} entries failed:")
        for nm, e in errors:
            print(f"  - {nm}: {e}")


def run_batch(client, system_prompt):
    entries = list(read_entries())
    print(f"BATCH mode: {len(entries)} entries, model={MODEL} (50% batch discount)\n")

    if WARM_CACHE_BEFORE_BATCH and entries:
        # One tiny call so the cached system prompt exists before the batch fans out,
        # otherwise parallel batch requests mostly miss the cache.
        print("Warming the prompt cache...")
        try:
            client.messages.create(
                model=MODEL, max_tokens=16, temperature=TEMPERATURE,
                system=system_blocks(system_prompt),
                messages=[{"role": "user", "content": "Reply with OK."}])
        except Exception as e:
            print(f"  (cache warm-up skipped: {e})")

    requests = [{
        "custom_id": cid,
        "params": {
            "model": MODEL, "max_tokens": MAX_TOKENS, "temperature": TEMPERATURE,
            "system": system_blocks(system_prompt),
            "messages": build_messages(user_msg),
        }} for cid, _, user_msg in entries]
    id_to_name = {cid: name for cid, name, _ in entries}
    id_to_msg = {cid: msg for cid, _, msg in entries}

    batch = client.messages.batches.create(requests=requests)
    print(f"Submitted batch {batch.id}. Polling every {POLL_SECONDS}s...")
    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        print(f"  status={b.processing_status}  counts={b.request_counts}")
        time.sleep(POLL_SECONDS)
    print(f"Batch ended. Final counts: {b.request_counts}\n")

    # Results can arrive in any order -> match on custom_id, then re-sort to input order.
    by_id, evidence_rows, errors = {}, [], []
    tot_in = tot_cr = tot_cw = tot_out = 0
    tot_cost = 0.0
    billed = 0
    total = len(entries)
    for n, entry in enumerate(client.messages.batches.results(batch.id), 1):
        name = id_to_name.get(entry.custom_id, entry.custom_id)
        if entry.result.type != "succeeded":
            errors.append((name, entry.result.type))
            print(f"[{n}/{total}] {entry.result.type.upper()}  {name}")
            continue
        result_msg = entry.result.message
        bi, cr, cw, out = usage_breakdown(result_msg.usage)
        cost = usage_cost(result_msg.usage, batched=True)
        tot_in += bi; tot_cr += cr; tot_cw += cw; tot_out += out
        tot_cost += cost; billed += 1
        usage_str = (f"in {bi} | cache_read {cr} | out {out} | "
                     f"${cost:.4f} | running ${tot_cost:.4f}")
        raw = extract_text(result_msg)
        row, ev, perr = parse_response(name, raw)
        if row is None:
            note = truncation_note(result_msg.stop_reason)
            dump_failure(name, raw, result_msg.stop_reason)
            errors.append((name, (perr or "parse failed") + note))
            print(f"[{n}/{total}] PARSE FAIL {name}  ({usage_str}){note}")
            continue
        gissues = _ground_row(row, ev, id_to_msg.get(entry.custom_id, ""))
        if gissues:
            row["_grounding"] = gissues
        by_id[entry.custom_id] = row
        if ev is not None:
            evidence_rows.append((name, ev))
        print(f"[{n}/{total}] ok  {name}  ({usage_str})")

    print_cost_summary(billed, tot_in, tot_cr, tot_cw, tot_out, tot_cost, batched=True)
    rows = [by_id[cid] for cid, _, _ in entries if cid in by_id]
    _write_with_grounding(rows, evidence_rows)
    if errors:
        print(f"\n{len(errors)} entries failed (resubmit these in a follow-up batch):")
        for nm, e in errors:
            print(f"  - {nm}: {e}")


def _field_multiset(field, value):
    """Normalize a multi-value field to an order- and duplicate-insensitive set, so a mere
    reordering (or a repeated term) is NOT counted as a disagreement. Every multi-value field
    is now ';'-separated -- Education_Locations stops, Alt_Name, disciplines, means, patronage,
    societies, degrees -- so all of them split on ';'. (A single-value field with no ';'
    simply becomes a one-element set, so this is safe for every compared field.)"""
    return frozenset(p.strip() for p in (value or "").split(";")
                     if p.strip() and p.strip() != "None")


def consensus_row(run_rows):
    """Build one row from several runs.

    A value is kept only when one candidate has a strict majority. A tie is the
    field's sentinel, never the first run. The disagreement list still names
    every run's value so a reviewer can choose one."""
    row, diffs = {}, []
    for col in COLUMNS:
        vals = [r[col] for r in run_rows]
        counts = Counter(vals)
        value, top_count = counts.most_common(1)[0]
        winners = [v for v, c in counts.items() if c == top_count]
        row[col] = ground.sentinel_for(col) if len(winners) > 1 else value
        if col in COMPARED_FIELDS and len({_field_multiset(col, v) for v in vals}) > 1:
            diffs.append((col, vals))
    return row, diffs


def _ground_row(row, evidence, user_msg):
    """Blank facts the entry does not support. Returns the issue strings."""
    content, sources, heading = ground.split_entry(user_msg or "")
    return ground.apply(row, evidence or {}, content, sources, heading)


MONTHS = {"January","February","March","April","May","June",
          "July","August","September","October","November","December"}
DAYS_IN_MONTH = {"January":31,"February":29,"March":31,"April":30,"May":31,"June":30,
                 "July":31,"August":31,"September":30,"October":31,"November":30,"December":31}

# Fields checked for a repeated token. Split on ';' only -- so "Philosophy; Scholastic
# Philosophy" is two distinct tokens and is NOT a duplicate.
DUPLICATE_CHECK_FIELDS = ["Father_Occupation", "Religion_Affiliation",
                          "Scientific_Disciplines", "Means_of_Support",
                          "Patronage_Types", "Instruments",
                          "Scientific_Societies_Membership"]

_SENTINELS = {"", "None", "Unknown"}


def _as_int(value):
    """Return the int in a cell, or None for a sentinel/non-numeric value."""
    v = (value or "").strip()
    if v in _SENTINELS:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def validate_row(row, disagreement_fields=()):
    """Return a list of issue strings for one row. Empty list == the row is GOOD.

    Mechanical checks only (plus any agreement disagreements passed in). Sentinel values
    (`Unknown` / `None`) are skipped rather than treated as errors -- they are the correct
    way to record a missing fact."""
    issues = []

    # --- dates and lifespan ---
    born, died = _as_int(row.get("Born_Year")), _as_int(row.get("Died_Year"))
    span = _as_int(row.get("Dates_Lifespan"))
    # (B.C. years are negative, so plain comparison and subtraction are correct throughout)
    if born is not None and died is not None:
        if born > died:
            issues.append(f"Born_Year ({born}) is after Died_Year ({died})")
        expected = died - born
        if span is not None and abs(span - expected) > 1:
            issues.append(f"Dates_Lifespan ({span}) != Died_Year - Born_Year ({expected})")
    if span is not None:
        if span < 20:
            issues.append(f"Dates_Lifespan ({span}) is under 20 -- check the dates")
        elif span > 105:
            issues.append(f"Dates_Lifespan ({span}) is over 105 -- check the dates")

    # --- months and days ---
    for prefix in ("Born", "Died"):
        month = (row.get(f"{prefix}_Month") or "").strip()
        if month not in _SENTINELS and month not in MONTHS:
            issues.append(f"{prefix}_Month ('{month}') is not a full English month name")
        day = _as_int(row.get(f"{prefix}_Day"))
        if day is not None:
            if not 1 <= day <= 31:
                issues.append(f"{prefix}_Day ({day}) is out of range 1-31")
            elif month in DAYS_IN_MONTH and day > DAYS_IN_MONTH[month]:
                issues.append(f"{prefix}_Day ({day}) is invalid for {month}")

    # --- coded fields ---
    wealth = (row.get("Father_Is_Wealthy") or "").strip()
    if wealth not in {"1", "0", "-1"}:
        issues.append(f"Father_Is_Wealthy ('{wealth}') must be 1, 0, or -1")
    for flag in ("Dates_Born_Certain", "Date_Died_Certain", "Religion_Affiliation_Certain"):
        val = (row.get(flag) or "").strip()
        if val not in {"0", "1"}:
            issues.append(f"{flag} ('{val}') must be 0 or 1")
    gender = (row.get("Gender") or "").strip()
    if gender not in {"Male", "Female", "Unknown"}:
        issues.append(f"Gender ('{gender}') must be Male, Female, or Unknown")

    # --- unmapped synonyms ---
    for field, lut in FIELD_SYNONYM_MAPS.items():
        for token in (t.strip() for t in (row.get(field) or "").split(";")):
            if token in _SENTINELS:
                continue
            target = lut.get(token.lower())
            if target and target != token:
                issues.append(f"{field}: '{token}' should be mapped to '{target}'")

    # --- specific terms must keep the parent a reader filters on ---
    for field, parents in FIELD_PARENT_MAPS.items():
        tokens = [t.strip() for t in (row.get(field) or "").split(";") if t.strip()]
        for token in tokens:
            parent = parents.get(token)
            if parent and parent not in tokens:
                issues.append(f"{field}: '{token}' is missing its parent '{parent}'")

    # --- duplicate tokens within a multi-value cell ---
    for field in DUPLICATE_CHECK_FIELDS:
        tokens = [t.strip() for t in (row.get(field) or "").split(";") if t.strip()]
        repeated = sorted({t for t in tokens if tokens.count(t) > 1})
        for term in repeated:
            issues.append(f"{field}: '{term}' appears more than once")

    # --- run-to-run disagreements (treated as issues, so the row is not GOOD) ---
    # Each item is one of:
    #   field name                          -> just the field
    #   (field, [v1, v2, ...])              -> field + each run's value
    #   (field, [v1, ...], [ev1, ...])      -> field + each run's value AND its evidence
    # When evidence is present (evidence mode was on), it prints beneath each run value as
    # the justification a reviewer can pick between.
    for item in disagreement_fields:
        if isinstance(item, (tuple, list)) and len(item) == 3:
            field, values, evidences = item
            issues.append(f"DISAGREEMENT {field}")
            for i, val in enumerate(values, 1):
                issues.append(f"    run{i}: {val}")
                ev = evidences[i - 1] if evidences and i - 1 < len(evidences) else None
                if ev:
                    issues.append(f"        evidence: {ev}")
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            field, values = item
            issues.append(f"DISAGREEMENT {field}")
            for i, val in enumerate(values, 1):
                issues.append(f"    run{i}: {val}")
        else:
            issues.append(f"DISAGREEMENT {item}")

    return issues


def write_validation_report(rows, diffs_by_index=None, extra_by_index=None):
    """Row-by-row report: 'GOOD' when a row passes every check, otherwise the issues.
    diffs_by_index maps a row's 0-based position -> [field, ...] that disagreed across runs
    (keyed positionally because the DSB heading and the parsed name need not match)."""
    diffs_by_index = diffs_by_index or {}
    extra_by_index = extra_by_index or {}
    total_issues = clean = 0
    issue_counts = Counter()
    with open(VALIDATION_REPORT, "w", encoding="utf-8") as f:
        f.write(f"VALIDATION REPORT  (model={MODEL}, temperature={TEMPERATURE})\n")
        f.write(f"Rows checked: {len(rows)}\n")
        f.write("Each row is GOOD or lists its issues. Run-to-run disagreements are\n")
        f.write("included as issues, so a row that wavered is never marked GOOD.\n")
        if not FIELD_SYNONYM_MAPS:
            f.write("NOTE: dsb_mappings.py not found -- the synonym check was skipped.\n")
        f.write("\n")
        for i, row in enumerate(rows, 1):
            name = f"{row.get('Last_Name','?')}, {row.get('First_Name','?')}"
            key = name.upper()
            issues = list(extra_by_index.get(i - 1, []))
            issues.extend(validate_row(row, diffs_by_index.get(i - 1, ())))
            if issues:
                # lines starting with spaces are run-value continuations of the issue
                # above them, not issues in their own right
                real = [x for x in issues if not x.startswith(" ")]
                total_issues += len(real)
                f.write(f"Row {i} [{key}]: {len(real)} issue(s)\n")
                for issue in issues:
                    if issue.startswith(" "):
                        f.write(f"    {issue}\n")
                    else:
                        f.write(f"    - {issue}\n")
                        issue_counts[issue.split(":")[0].split(" (")[0]] += 1
            else:
                clean += 1
                f.write(f"Row {i} [{key}]: GOOD\n")
        f.write(f"\n--- summary ---\n")
        f.write(f"  clean rows:   {clean}/{len(rows)}\n")
        f.write(f"  rows flagged: {len(rows) - clean}\n")
        f.write(f"  total issues: {total_issues}\n")
        if issue_counts:
            f.write("  most common:\n")
            for label, n in issue_counts.most_common(12):
                f.write(f"     {n:4}  {label}\n")
    print(f"Validation report written to {VALIDATION_REPORT} "
          f"({clean}/{len(rows)} rows clean)")


def _agreement_finalize(entries, rows_by_cid, evidence_rows, cost, runs, batched,
                        evidence_by_cid=None):
    """Shared tail for both agreement paths. Takes {custom_id: [run_rows]} and builds the
    consensus output + the agreement report (every disagreement across the compared fields,
    order-insensitive), then writes outputs and the cost summary.

    evidence_by_cid maps {custom_id: [per-run _evidence dict]}, aligned run-for-run with
    rows_by_cid. When present (evidence mode on), each disagreement carries the phrase each
    run cited, so the validation report shows a justification to pick between."""
    billed, tot_in, tot_cr, tot_cw, tot_out, tot_cost = cost
    evidence_by_cid = evidence_by_cid or {}
    id_to_name = {cid: name for cid, name, _ in entries}
    rows = []
    per_entry_diffs = []                 # (name, [(field, [values...]), ...])
    diffs_by_index = {}                  # output row index -> [(field, [vals], [evid]), ...]
    field_volatility = Counter()         # how many entries disagreed on each field
    for cid, name, _ in entries:
        run_rows = rows_by_cid.get(cid, [])
        if not run_rows:
            print(f"  {name}: no successful runs")
            continue
        row, diffs = consensus_row(run_rows)
        kept_ev = {}
        run_evs = evidence_by_cid.get(cid, [])
        for col in COLUMNS:
            if ground.is_sentinel(col, row.get(col)):
                continue
            for i, run in enumerate(run_rows):
                if str(run.get(col)) == str(row.get(col)) and i < len(run_evs) and run_evs[i]:
                    kept_ev[col] = run_evs[i].get(col, "")
                    break
        user_msg = next((msg for eid, _, msg in entries if eid == cid), "")
        gissues = _ground_row(row, kept_ev, user_msg)
        if gissues:
            # filled in below once the list exists; stash on the row temporarily
            row["_grounding"] = gissues
        row["Disagreement_Count"] = str(len(diffs))
        row["Inconsistent_Fields"] = "; ".join(f for f, _ in diffs) if diffs else "None"
        if diffs:
            run_evs = evidence_by_cid.get(cid, [])
            enriched = []
            for f, vals in diffs:
                evid = [(run_evs[i].get(f) if i < len(run_evs) and run_evs[i] else None)
                        for i in range(len(vals))]
                enriched.append((f, vals, evid))
            diffs_by_index[len(rows)] = enriched
        rows.append(row)
        for field, _v in diffs:
            field_volatility[field] += 1
        if diffs:
            per_entry_diffs.append((name, diffs))

    with open(AGREEMENT_REPORT, "w", encoding="utf-8") as f:
        f.write(f"AGREEMENT REPORT  ({runs} runs/entry, temperature={TEMPERATURE}, model={MODEL}"
                f"{', batch' if batched else ''})\n")
        f.write(f"Entries checked: {len(entries)}\n")
        f.write("Every disagreement across the compared fields is listed. Comparison ignores\n")
        f.write("ordering and duplicates, so a mere reordering is not counted. Free-text\n")
        f.write("'_Notes' and source fields are excluded (they vary in wording by design).\n")
        f.write("A tie is written as the field's sentinel. It is not resolved by keeping run 1.\n\n")
        f.write("FIELD VOLATILITY (entries where the runs disagreed on this field):\n")
        if field_volatility:
            for field, c in field_volatility.most_common():
                f.write(f"  {c}/{len(entries)}  {field}\n")
        else:
            f.write("  (none - every field agreed on every entry)\n")
        f.write("\nPER-ENTRY DISAGREEMENTS (review these cells):\n")
        for name, diffs in per_entry_diffs:
            f.write(f"\n=== {name} ===\n")
            for field, vals in diffs:
                f.write(f"  {field}:\n")
                for i, v in enumerate(vals, 1):
                    f.write(f"     run{i}: {v}\n")
    print(f"\nAgreement report written to {AGREEMENT_REPORT}")
    grounding_by_index = {}
    for i, row in enumerate(rows):
        gissues = row.pop("_grounding", None)
        if gissues:
            grounding_by_index[i] = gissues
    write_validation_report(rows, diffs_by_index, grounding_by_index)
    print_cost_summary(billed, tot_in, tot_cr, tot_cw, tot_out, tot_cost, batched=batched)
    write_outputs(rows, evidence_rows, collect_new_terms(rows),
                  extra_columns=AGREEMENT_FLAG_COLUMNS)


def run_agreement(client, system_prompt):
    """Reliability check: extract each entry AGREEMENT_RUNS times and report which fields
    disagree across runs. Agreement => high confidence; disagreement => review. Set
    AGREEMENT_BATCH = True to run the whole thing through the Batches API (50% off)."""
    if AGREEMENT_BATCH:
        return run_agreement_batch(client, system_prompt)

    all_entries = list(read_entries())
    entries = all_entries if not TEST_LIMIT else all_entries[:TEST_LIMIT]
    runs = max(2, AGREEMENT_RUNS)
    total = len(entries)
    print(f"AGREEMENT mode: {total} entries x {runs} runs (synchronous), "
          f"model={MODEL}, temperature={TEMPERATURE}\n")
    rows_by_cid, evidence_rows = {}, []
    evidence_by_cid = {}                  # cid -> [per-run evidence dict], aligned to run_rows
    tot_in = tot_cr = tot_cw = tot_out = 0
    tot_cost = 0.0
    billed = 0
    for n, (cid, name, user_msg) in enumerate(entries, 1):
        run_rows, run_evs = [], []
        for k in range(runs):
            try:
                msg = client.messages.create(
                    model=MODEL, max_tokens=MAX_TOKENS, temperature=TEMPERATURE,
                    system=system_blocks(system_prompt),
                    messages=build_messages(user_msg))
            except Exception as e:
                print(f"[{n}/{total}] run {k + 1} API error  {name}: {e}")
                continue
            bi, cr, cw, out = usage_breakdown(msg.usage)
            tot_in += bi; tot_cr += cr; tot_cw += cw; tot_out += out
            tot_cost += usage_cost(msg.usage, batched=False); billed += 1
            row, ev, perr = parse_response(name, extract_text(msg))
            if row is not None:
                run_rows.append(row)
                run_evs.append(ev)                       # ev is None when evidence mode off
                if ev is not None and k == 0:
                    evidence_rows.append((name, ev))
        rows_by_cid[cid] = run_rows
        evidence_by_cid[cid] = run_evs
        if not run_rows:
            print(f"[{n}/{total}] {name}: all {runs} runs failed to parse")
            continue
        _, diffs = consensus_row(run_rows)
        if diffs:
            agree = len(COMPARED_FIELDS) - len(diffs)
            print(f"[{n}/{total}] {name}: {agree}/{len(COMPARED_FIELDS)} fields agree | "
                  f"differ: {', '.join(f for f, _ in diffs)}")
        else:
            print(f"[{n}/{total}] {name}: all {len(COMPARED_FIELDS)} compared fields agree")

    _agreement_finalize(entries, rows_by_cid, evidence_rows,
                        (billed, tot_in, tot_cr, tot_cw, tot_out, tot_cost), runs,
                        batched=False, evidence_by_cid=evidence_by_cid)


def run_agreement_batch(client, system_prompt):
    """Agreement mode through the Batches API: submit all (entry x run) requests at once for
    the 50% discount and parallel processing, then group results per entry and finalize."""
    all_entries = list(read_entries())
    entries = all_entries if not TEST_LIMIT else all_entries[:TEST_LIMIT]
    runs = max(2, AGREEMENT_RUNS)
    total = len(entries)
    print(f"AGREEMENT mode (batch): {total} entries x {runs} runs = {total * runs} requests, "
          f"model={MODEL} (50% batch discount)\n")

    if WARM_CACHE_BEFORE_BATCH and entries:
        print("Warming the prompt cache...")
        try:
            client.messages.create(
                model=MODEL, max_tokens=16, temperature=TEMPERATURE,
                system=system_blocks(system_prompt),
                messages=[{"role": "user", "content": "Reply with OK."}])
        except Exception as e:
            print(f"  (cache warm-up skipped: {e})")

    # One request per (entry, run); custom_id carries both so results can be regrouped.
    requests = []
    for cid, _, user_msg in entries:
        for k in range(runs):
            requests.append({
                "custom_id": f"{cid}__run{k}",
                "params": {
                    "model": MODEL, "max_tokens": MAX_TOKENS, "temperature": TEMPERATURE,
                    "system": system_blocks(system_prompt),
                    "messages": build_messages(user_msg),
                }})
    id_to_name = {cid: name for cid, name, _ in entries}

    batch = client.messages.batches.create(requests=requests)
    print(f"Submitted batch {batch.id} ({len(requests)} requests). Polling every {POLL_SECONDS}s...")
    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        print(f"  status={b.processing_status}  counts={b.request_counts}")
        time.sleep(POLL_SECONDS)
    print(f"Batch ended. Final counts: {b.request_counts}\n")

    rows_by_cid = defaultdict(list)
    evidence_by_cid = defaultdict(list)  # aligned run-for-run with rows_by_cid
    evidence_rows = []
    tot_in = tot_cr = tot_cw = tot_out = 0
    tot_cost = 0.0
    billed = 0
    for entry in client.messages.batches.results(batch.id):
        base_cid = entry.custom_id.rsplit("__run", 1)[0]
        name = id_to_name.get(base_cid, base_cid)
        if entry.result.type != "succeeded":
            print(f"  {entry.custom_id}: {entry.result.type}")
            continue
        result_msg = entry.result.message
        bi, cr, cw, out = usage_breakdown(result_msg.usage)
        tot_in += bi; tot_cr += cr; tot_cw += cw; tot_out += out
        tot_cost += usage_cost(result_msg.usage, batched=True); billed += 1
        row, ev, perr = parse_response(name, extract_text(result_msg))
        if row is None:
            dump_failure(name, extract_text(result_msg), result_msg.stop_reason)
            continue
        rows_by_cid[base_cid].append(row)
        evidence_by_cid[base_cid].append(ev)         # aligned with the row just appended
        if ev is not None and entry.custom_id.endswith("__run0"):
            evidence_rows.append((name, ev))

    _agreement_finalize(entries, rows_by_cid, evidence_rows,
                        (billed, tot_in, tot_cr, tot_cw, tot_out, tot_cost), runs,
                        batched=True, evidence_by_cid=evidence_by_cid)


def run_verify(client, system_prompt):
    """Production run for scaling up.

    Each entry is extracted ONCE -- that pass is the output row for ALL 40 fields. Then only
    the volatile VERIFY_FIELDS are re-checked with a second, narrowed call. Wherever the two
    passes disagree on those fields, the entry is written to AGREEMENT_REPORT for a human
    reviewer. Most of the schema is produced in a single pass; only the fields we know to be
    volatile pay for a second look, and the extra output is small (three fields, not forty)."""
    all_entries = list(read_entries())
    entries = all_entries if not TEST_LIMIT else all_entries[:TEST_LIMIT]
    total = len(entries)
    kind = "narrowed" if VERIFY_NARROW else "full"
    print(f"VERIFY mode: {total} entries, single-pass output + {kind} second check on "
          f"{', '.join(VERIFY_FIELDS)}, model={MODEL}\n")

    verify_suffix = (
        "\n\n---\nFor THIS response ONLY, narrow your output: return a JSON object with "
        "EXACTLY these keys and no others, applying the usual rules for each field: "
        + ", ".join(f'"{f}"' for f in VERIFY_FIELDS)
        + ". Ignore all other fields for this response."
    ) if VERIFY_NARROW else ""

    rows, evidence_rows, errors = [], [], []
    per_entry_diffs = []                 # (name, [(field, output_val, verify_val), ...])
    field_volatility = Counter()
    tot_in = tot_cr = tot_cw = tot_out = 0
    tot_cost = 0.0
    entries_done = 0

    for n, (cid, name, user_msg) in enumerate(entries, 1):
        # --- Primary pass: full extraction -> the output row ---
        try:
            msg = client.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS, temperature=TEMPERATURE,
                system=system_blocks(system_prompt),
                messages=build_messages(user_msg))
        except Exception as e:
            errors.append((name, str(e)))
            print(f"[{n}/{total}] API error  {name}: {e}")
            continue
        bi, cr, cw, out = usage_breakdown(msg.usage)
        tot_in += bi; tot_cr += cr; tot_cw += cw; tot_out += out
        tot_cost += usage_cost(msg.usage, batched=False)
        raw = extract_text(msg)
        row, ev, perr = parse_response(name, raw)
        if row is None:
            note = truncation_note(msg.stop_reason)
            dump_failure(name, raw, msg.stop_reason)
            errors.append((name, (perr or "parse failed") + note))
            print(f"[{n}/{total}] PARSE FAIL {name}{note}")
            continue
        gissues = _ground_row(row, ev, user_msg)
        if gissues:
            row["_grounding"] = gissues
        rows.append(row)
        if ev is not None:
            evidence_rows.append((name, ev))
        entries_done += 1

        # --- Verification pass: re-check only the volatile fields ---
        vrow = None
        try:
            vmsg = client.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS, temperature=TEMPERATURE,
                system=system_blocks(system_prompt),
                messages=build_messages(user_msg + verify_suffix))
            bi, cr, cw, out = usage_breakdown(vmsg.usage)
            tot_in += bi; tot_cr += cr; tot_cw += cw; tot_out += out
            tot_cost += usage_cost(vmsg.usage, batched=False)
            vrow, _, _ = parse_response(name, extract_text(vmsg))
        except Exception as e:
            print(f"[{n}/{total}] verify call failed for {name}: {e}")

        diffs = []
        if vrow is not None:
            for field in VERIFY_FIELDS:
                if _field_multiset(field, row.get(field, "")) != _field_multiset(field, vrow.get(field, "")):
                    diffs.append((field, row.get(field, ""), vrow.get(field, "")))
                    field_volatility[field] += 1
        if diffs:
            per_entry_diffs.append((name, diffs))
            print(f"[{n}/{total}] {name}: differs on {', '.join(f for f, _, _ in diffs)}")
        else:
            print(f"[{n}/{total}] {name}: volatile fields agree")

    # Verification report (written to the agreement-report path).
    with open(AGREEMENT_REPORT, "w", encoding="utf-8") as f:
        f.write("VERIFICATION REPORT  (single-pass output; volatile fields re-checked once)\n")
        f.write(f"model={MODEL}, temperature={TEMPERATURE}\n")
        f.write(f"Re-checked fields: {', '.join(VERIFY_FIELDS)}\n")
        f.write(f"Entries checked: {total}\n")
        f.write("The output CSV uses the FIRST pass for every field. Listed below are the\n")
        f.write("entries where the second pass disagreed on a re-checked field -- review\n")
        f.write("these cells against the DSB source. (Comparison ignores ordering and\n")
        f.write("duplicates, so a mere reordering is not flagged.)\n\n")
        f.write("FIELD VOLATILITY (entries where the two passes disagreed):\n")
        if field_volatility:
            for field, c in field_volatility.most_common():
                f.write(f"  {c}/{total}  {field}\n")
        else:
            f.write("  (none - the two passes agreed on every re-checked field)\n")
        f.write("\nPER-ENTRY DISAGREEMENTS (output = the value written to the CSV):\n")
        for name, diffs in per_entry_diffs:
            f.write(f"\n=== {name} ===\n")
            for field, v_out, v_ver in diffs:
                f.write(f"  {field}:\n")
                f.write(f"     output: {v_out}\n")
                f.write(f"     verify: {v_ver}\n")
    print(f"\nVerification report written to {AGREEMENT_REPORT}")
    print_cost_summary(entries_done, tot_in, tot_cr, tot_cw, tot_out, tot_cost,
                       batched=False, project_to=len(all_entries))
    _write_with_grounding(rows, evidence_rows)
    if errors:
        print(f"\n{len(errors)} entries failed:")
        for nm, e in errors:
            print(f"  - {nm}: {e}")


def volume_tag():
    """'_vol01' for VOLUME = 1, '_vol12' for 12, '_vol01b' for '01b'."""
    try:
        return f"_vol{int(VOLUME):02d}"
    except (TypeError, ValueError):
        return f"_vol{str(VOLUME).strip()}"


def archive_outputs():
    """Copy everything this run produced into ARCHIVE_DIR, renamed with the volume tag:
    dsb_extracted.csv -> archive/dsb_extracted_vol01.csv

    Copies rather than moves, so the working files stay in place for the standalone
    validator and any follow-up step. Re-running the same volume overwrites that volume's
    archived copies, which is what you want on a re-run; a different VOLUME never collides."""
    tag = volume_tag()
    produced = [OUTPUT_CSV, AGREEMENT_REPORT, VALIDATION_REPORT,
                NEW_TERMS_REPORT, FAILURE_LOG, EVIDENCE_CSV]
    if ARCHIVE_PROMPT:
        produced.append(SYSTEM_PROMPT_PATH)     # the spec that produced these rows

    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
    except OSError as e:
        print(f"\nCould not create {ARCHIVE_DIR}/ -- outputs left in place ({e})")
        return

    print(f"\n--- archiving to {ARCHIVE_DIR}/ (volume tag '{tag}') ---")
    archived = 0
    for path in produced:
        if not path or not os.path.exists(path):
            continue                            # not every run produces every file
        stem, ext = os.path.splitext(os.path.basename(path))
        dest = os.path.join(ARCHIVE_DIR, f"{stem}{tag}{ext}")
        try:
            shutil.copy2(path, dest)
            print(f"  {os.path.basename(path):28} -> {dest}")
            archived += 1
        except OSError as e:
            print(f"  could not archive {path}: {e}")
    if not archived:
        print("  (nothing to archive -- no output files found)")


def main():
    global VOLUME
    if len(sys.argv) > 1:                       # e.g. python dsb_batch_extract.py 3
        VOLUME = sys.argv[1]
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY in your environment first.")
    client = anthropic.Anthropic()
    system_prompt = load_system_prompt()
    # The failure log is append-mode; clear it so it reflects THIS run only (otherwise a
    # previous volume's failures would be archived under this volume's tag).
    if os.path.exists(FAILURE_LOG):
        os.remove(FAILURE_LOG)
    print(f"Volume {volume_tag().replace('_vol', '')} | mode={MODE} | model={MODEL}\n")
    if MODE == "test":
        run_test(client, system_prompt)
    elif MODE == "batch":
        run_batch(client, system_prompt)
    elif MODE == "agreement":
        run_agreement(client, system_prompt)
    elif MODE == "verify":
        run_verify(client, system_prompt)
    else:
        sys.exit('MODE must be "test", "batch", "agreement", or "verify".')
    archive_outputs()


if __name__ == "__main__":
    main()