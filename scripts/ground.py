#!/usr/bin/env python3
"""
Decide which extracted values are allowed to stand.

A value survives only when the words cited for it are actually in the entry.
A year, month, day, or town has to be supported by those words. Two runs that
disagree never reach this step with a winner: the extractor writes the field's
sentinel instead of picking a run. A lifespan that contradicts the two years
clears both years.

Notes are paraphrases. When the fact they describe is rejected, the note is
cleared with it, so a rejected fact cannot survive in prose.
"""

import re
import unicodedata

_NONE_FIELDS = {
    "Alt_Name", "Education_Institution", "Education_Degrees", "Education_Locations",
    "Instruments", "Scientific_Societies_Membership", "Sources_List",
    "Not_Available_and_Not_Consulted_List", "Compiler_Details_Notes",
}
_NOTE_OF = {
    "Father_Occupation": "Father_Notes",
    "Education_Degrees": "Education_Notes",
    "Education_Locations": "Education_Notes",
    "Religion_Affiliation": "Religion_Notes",
    "Scientific_Disciplines": "Scientific_Disciplines_Notes",
    "Means_of_Support": "Means_of_Support_Notes",
    "Patronage_Types": "Patronage_Notes",
    "Instruments": "Instruments_Notes",
    "Scientific_Societies_Membership": "Scientific_Societies_Notes",
}
# Facts a quotation must support. Notes are checked only so they cannot
# outlive the fact they describe.
_GROUNDED_FIELDS = {
    "Alt_Name", "Gender",
    "Born_Country", "Born_Town", "Born_Year", "Born_Month", "Born_Day",
    "Died_Country", "Died_Town", "Died_Year", "Died_Month", "Died_Day",
    "Father_Occupation", "Father_Is_Wealthy",
    "Education_Degrees", "Education_Locations",
    "Religion_Affiliation",
    "Scientific_Disciplines", "Means_of_Support", "Patronage_Types",
    "Instruments", "Scientific_Societies_Membership",
    "Sources_List", "Compiler_Details_Notes",
}
_YEAR_FIELDS = {"Born_Year", "Died_Year"}
_MONTH_FIELDS = {"Born_Month", "Died_Month"}
_DAY_FIELDS = {"Born_Day", "Died_Day"}
_TOWN_FIELDS = {"Born_Town", "Died_Town"}
_COUNTRY_FIELDS = {"Born_Country", "Died_Country"}
_MONTHS = ("january", "february", "march", "april", "may", "june",
           "july", "august", "september", "october", "november", "december")
_HEDGE = re.compile(r"\b(ca\.?|c\.|circa|about|around|\?)\b", re.I)
_PRONOUN = {
    "Male": re.compile(r"\b(he|him|his)\b", re.I),
    "Female": re.compile(r"\b(she|her|hers)\b", re.I),
}


def sentinel_for(field):
    """The value this field holds when the entry does not support a fact."""
    if field == "Father_Is_Wealthy":
        return "-1"
    if field.endswith("_Certain"):
        return "0"
    if field.endswith("_Notes") or field in _NONE_FIELDS:
        return "None"
    if field == "Disagreement_Count":
        return "0"
    if field == "Inconsistent_Fields":
        return "None"
    return "Unknown"


def is_sentinel(field, value):
    text = "" if value is None else str(value).strip()
    if text == "" or text == sentinel_for(field):
        return True
    return text in {"Unknown", "None"} and sentinel_for(field) in {"Unknown", "None"}


def fold(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("\\n", "\n")
    text = re.sub(r"\s+", " ", text)
    return text.casefold().strip()


def _tokens(text):
    return re.findall(r"[a-z0-9]+", fold(text))


def _distance(a, b):
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 2:
        return 3
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(cur[-1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def in_text(needle, *parts):
    """True when needle is a contiguous stretch of the entry, ignoring case and accents."""
    hay = fold(" ".join(p for p in parts if p))
    pin = fold(needle)
    return bool(pin) and pin in hay


def near_in_text(needle, *parts):
    """A place name matches as printed, or with a one- or two-letter misprint."""
    if in_text(needle, *parts):
        return True
    pin = fold(needle)
    if len(pin) < 5:
        return False
    for word in _tokens(" ".join(p for p in parts if p)):
        if _distance(pin, word) <= 2:
            return True
    return False


def split_entry(user_msg):
    """Pull the heading, the article, and the bibliography out of an extractor message."""
    heading, content, sources = "", "", ""
    text = user_msg or ""
    if text.startswith("NAME:"):
        first, _, rest = text.partition("\n")
        heading = first[5:].strip()
        text = rest
    if "CONTENT:" in text:
        _, _, text = text.partition("CONTENT:")
    if "SOURCES:" in text:
        content, _, sources = text.partition("SOURCES:")
    else:
        content = text
    return content.strip(), sources.strip(), heading.strip()


def _blank(row, field, issues, reason):
    row[field] = sentinel_for(field)
    issues.append(f"{field}: {reason}; left {sentinel_for(field)}")
    note = _NOTE_OF.get(field)
    if note and not is_sentinel(note, row.get(note)):
        row[note] = sentinel_for(note)
        issues.append(f"{note}: cleared because {field} was not kept")


def _quote_ok(evidence, content, sources):
    quote = (evidence or "").strip()
    if not quote or quote.casefold() in {"absent", "inferred"}:
        return False
    return in_text(quote, content, sources)


def _year_in_quote(year, quote):
    digits = str(abs(int(year)))
    if not re.search(rf"(?<!\d){digits}(?!\d)", quote or ""):
        return False
    if int(year) < 0 and not re.search(r"\bb\.?\s*c\.?\b", quote or "", re.I):
        return False
    return True


def apply(row, evidence, content, sources, heading=""):
    """Blank every fact the entry does not support. Mutates row. Returns issue strings."""
    evidence = evidence or {}
    issues = []
    for field in list(_GROUNDED_FIELDS):
        if field not in row or is_sentinel(field, row.get(field)):
            continue
        ev = str(evidence.get(field, "") or "").strip()
        value = str(row.get(field)).strip()
        if ev.casefold() == "inferred":
            if not _inferred_ok(field, value, row, content, sources):
                _blank(row, field, issues, "inference is not one this check allows")
            continue
        if not _quote_ok(ev, content, sources):
            _blank(row, field, issues, "quotation is not in the entry")
            continue
        if field in _YEAR_FIELDS and not _year_in_quote(value, ev):
            _blank(row, field, issues, f"the quotation does not contain the year {value}")
        elif field in _MONTH_FIELDS and fold(value) not in fold(ev):
            _blank(row, field, issues, f"the quotation does not contain {value}")
        elif field in _DAY_FIELDS and not re.search(rf"(?<!\d){int(value)}(?!\d)", ev):
            _blank(row, field, issues, f"the quotation does not contain the day {value}")
        elif field in _TOWN_FIELDS and not near_in_text(value.split(",")[0], content, sources):
            _blank(row, field, issues, "the town is not in the entry")
        elif field == "Education_Locations" and not _education_ok(value, content, sources):
            _blank(row, field, issues, "a study stop is not supported by the entry")
        elif field == "Compiler_Details_Notes":
            author = _author_name(value)
            if author and not in_text(author, content, sources, heading):
                _blank(row, field, issues, "the entry author is not printed in the entry")

    _names(row, heading, issues)
    _dates(row, issues)
    _certainty(row, evidence)
    _lifespan(row)
    return issues


def _inferred_ok(field, value, row, content, sources):
    if field == "Gender":
        pattern = _PRONOUN.get(value)
        return bool(pattern and pattern.search(f"{content}\n{sources}"))
    if field in _COUNTRY_FIELDS:
        which = "Born" if field.startswith("Born") else "Died"
        town = str(row.get(f"{which}_Town", "") or "")
        return not is_sentinel(f"{which}_Town", town) and near_in_text(town.split(",")[0], content, sources)
    return False


def _education_ok(value, content, sources):
    for stop in (part.strip() for part in value.split(";")):
        if not stop or stop in {"None", "Unknown"}:
            continue
        place = stop.split("@", 1)[1] if "@" in stop else stop
        bits = [b.strip() for b in place.split(",")]
        town = bits[0] if bits else ""
        if town and town not in {"Unknown", "None"} and not near_in_text(town, content, sources):
            return False
        for bit in bits[3:5]:
            if bit.lstrip("-").isdigit() and not re.search(rf"(?<!\d){abs(int(bit))}(?!\d)", f"{content}\n{sources}"):
                return False
    return True


def _author_name(note):
    match = re.search(r"entry by\s+(.+?)\.?$", note or "", re.I)
    if not match:
        return ""
    return match.group(1).strip(" .")


def _names(row, heading, issues):
    if not heading:
        return
    hay = fold(heading)
    last = str(row.get("Last_Name", "") or "").strip()
    first = str(row.get("First_Name", "") or "").strip()
    if last and last not in {"Unknown", "None"} and fold(last) not in hay:
        issues.append(f"Last_Name: {last!r} is not in the heading {heading!r}")
    if first and first not in {"Unknown", "None"} and fold(first) not in hay:
        issues.append(f"First_Name: {first!r} is not in the heading {heading!r}")


def _dates(row, issues):
    def year(field):
        try:
            return int(str(row.get(field)).strip())
        except (TypeError, ValueError):
            return None
    born, died = year("Born_Year"), year("Died_Year")
    if born is None or died is None:
        return
    span = died - born
    if born > died or span < 20 or span > 105:
        for field in ("Born_Year", "Died_Year", "Dates_Lifespan"):
            if field in row:
                row[field] = sentinel_for(field)
        for field in ("Dates_Born_Certain", "Date_Died_Certain"):
            if field in row:
                row[field] = "0"
        issues.append(
            f"Born_Year: printed years {born} and {died} imply a lifespan of {span}; "
            "both years left Unknown"
        )


def _certainty(row, evidence):
    evidence = evidence or {}
    for year_field, flag in (("Born_Year", "Dates_Born_Certain"), ("Died_Year", "Date_Died_Certain")):
        if is_sentinel(year_field, row.get(year_field)):
            if flag in row:
                row[flag] = "0"
            continue
        if _HEDGE.search(str(evidence.get(year_field, "") or "")) and flag in row:
            row[flag] = "0"


def _lifespan(row):
    if "Dates_Lifespan" not in row:
        return
    try:
        born = int(str(row.get("Born_Year")).strip())
        died = int(str(row.get("Died_Year")).strip())
    except (TypeError, ValueError):
        row["Dates_Lifespan"] = "Unknown"
        return
    row["Dates_Lifespan"] = str(died - born)


def _self_test():
    text = "He was born at Gottiigen in 1789 and died in 1959 after a long life."
    row = {
        "Born_Year": "1789", "Died_Year": "1959", "Dates_Lifespan": "70",
        "Dates_Born_Certain": "1", "Date_Died_Certain": "1",
        "Born_Town": "Göttingen", "Born_Country": "Germany",
        "Gender": "Male", "Scientific_Disciplines": "Mathematics",
        "Scientific_Disciplines_Notes": "Wrote on algebra.",
        "Compiler_Details_Notes": "Dictionary of Scientific Biography entry by Ada Lovelace.",
    }
    ev = {
        "Born_Year": "born at Gottiigen in 1789",
        "Died_Year": "died in 1959",
        "Born_Town": "born at Gottiigen",
        "Born_Country": "inferred",
        "Gender": "inferred",
        "Scientific_Disciplines": "algebra was his life",
        "Compiler_Details_Notes": "entry by Ada Lovelace",
    }
    issues = apply(row, ev, text, "", "Gauss, Carl Friedrich")
    assert row["Born_Year"] == "Unknown" and row["Died_Year"] == "Unknown", row
    assert row["Dates_Lifespan"] == "Unknown"
    assert any("lifespan" in i for i in issues)
    assert row["Gender"] == "Male"
    assert row["Born_Town"] == "Göttingen"
    assert row["Scientific_Disciplines"] == "Unknown"
    assert row["Scientific_Disciplines_Notes"] == "None"
    assert row["Compiler_Details_Notes"] == "None"

    kept = {"Born_Year": "1720", "Died_Year": "1760", "Dates_Lifespan": "70",
            "Dates_Born_Certain": "1", "Date_Died_Certain": "1"}
    # A short, possible life stays, and a hedge clears certainty.
    short = "born in 1720 and died ca. 1760"
    kept_row = dict(kept)
    apply(kept_row, {"Born_Year": "born in 1720", "Died_Year": "died ca. 1760"}, short, "")
    assert kept_row["Born_Year"] == "1720" and kept_row["Died_Year"] == "1760", kept_row
    assert kept_row["Dates_Lifespan"] == "40"
    assert kept_row["Date_Died_Certain"] == "0"
    print("ground.py self-test: OK")


if __name__ == "__main__":
    _self_test()
