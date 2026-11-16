#!/usr/bin/env python3
"""
Facet lists and degree spelling aliases.

A phrase is the entry's own word and is not listed here. A facet is the closed
filter beside that phrase. Discipline facets come from prompt/dictionary.json,
not from this file. Society names do too.

Degree spelling aliases join two spellings of one diploma (B.Sc. with B.S.).
They do not join different national diplomas. DEGREE_LEVEL records the level
only when the diploma's own name states it. Laurea, Licence, and Diplom are
absent on purpose.
"""

FIELDS = {
    "Gender": {
        "intro": "**Gender:**",
        "groups": {"": ["Male", "Female", "Unknown"]},
        "synonyms": {},
        "notes": "From a pronoun, title, or kinship word that refers to the subject. Otherwise `Unknown`.",
        "inline": True,
    },
    "Father_Occupation_Facet": {
        "intro": (
            "**Father_Occupation_Facet:** one closed grouping, or `None` when the "
            "phrase has no fitting facet. The phrase itself goes in `Father_Occupation`."
        ),
        "groups": {"": [
            "Land", "State", "Arms", "Church", "Medicine", "Law", "Learning",
            "Commerce", "Craft", "Arts", "Labor",
        ]},
        "synonyms": {},
        "notes": "A blank facet is `None`. Do not invent a facet for a phrase that does not fit.",
    },
    "Religion_Facet": {
        "intro": (
            "**Religion_Facet:** the grouping a reader filters on. The entry's own "
            "word stays in `Religion_Affiliation`."
        ),
        "groups": {"": [
            "Catholic", "Protestant", "Orthodox", "Jewish", "Muslim", "Hindu",
            "Buddhist", "Zoroastrian", "Quaker", "Unitarian", "Anabaptist",
            "Deist", "Agnostic", "Atheist", "Heterodox",
        ]},
        "synonyms": {},
        "notes": (
            "Quaker, Unitarian, and Anabaptist are their own facets. "
            "`Christian`, `Evangelical`, and `Reformed` are phrases with facet `None`."
        ),
    },
    "Means_of_Support_Facet": {
        "intro": (
            "**Means_of_Support_Facet:** the livelihood, aligned with each phrase "
            "in `Means_of_Support`. `None` for a phrase that does not fit."
        ),
        "groups": {"": [
            "Academia", "Government", "Church Life", "Medicine", "Military",
            "Commerce", "Craft", "Industry", "Publishing", "Writing", "Banking",
            "Mining", "Agriculture", "Engineering", "Personal Means", "Patronage",
            "Schoolmastering",
        ]},
        "synonyms": {},
        "notes": "A discipline name is not a facet. Silence in the phrase field is `Unknown`, not a guessed facet.",
    },
    "Patronage_Facet": {
        "intro": (
            "**Patronage_Facet:** the kind of patron, aligned with each name in "
            "`Patronage_Types`."
        ),
        "groups": {"": [
            "Court", "Government", "Aristocrat", "Church", "Academic",
            "Scientist", "Merchant", "Scientific Society", "Foundation", "City",
        ]},
        "synonyms": {},
        "notes": "One described patron is one facet. Do not add a second facet by inference.",
    },
    "Education_Institution_Facet": {
        "intro": (
            "**Education_Institution_Facet:** the kind of school, aligned with each "
            "stop. The school's name is the phrase."
        ),
        "groups": {"": [
            "University", "College", "Medical School", "School", "Seminary", "Academy",
        ]},
        "synonyms": {},
        "notes": "`Not Specified` has facet `None`.",
    },
    "Education_Degree_Facet": {
        "intro": (
            "**Education_Degree_Facet:** Bachelor, Master, Doctorate, or Professional, "
            "and only when the diploma's name states that level. Otherwise `None`."
        ),
        "groups": {"": ["Bachelor", "Master", "Doctorate", "Professional"]},
        "synonyms": {},
        "notes": "The phrase keeps the diploma the entry names. This facet does not rename it.",
    },
}

# Two spellings of the same diploma. The phrase is rewritten only for these.
DEGREE_SPELLING = {
    "a.b.": "B.A.",
    "b.sc.": "B.S.",
    "b.sc": "B.S.",
    "m.sc.": "M.S.",
    "m.sc": "M.S.",
    "d.phil.": "D.Phil.",
    "ph.d": "Ph.D.",
}

# Level stated by the diploma's own name. Absence means the facet stays unset.
DEGREE_LEVEL = {
    "B.A.": "Bachelor",
    "B.S.": "Bachelor",
    "B.D.": "Bachelor",
    "LL.B.": "Bachelor",
    "M.B.": "Bachelor",
    "Ch.B.": "Bachelor",
    "M.A.": "Master",
    "M.S.": "Master",
    "Ph.D.": "Doctorate",
    "M.D.": "Doctorate",
    "D.Sc.": "Doctorate",
    "D.D.": "Doctorate",
    "LL.D.": "Doctorate",
    "J.D.": "Doctorate",
    "Dr. phil.": "Doctorate",
    "Dr. med.": "Doctorate",
    "Dr. rer. nat.": "Doctorate",
    "Dr.-Ing.": "Doctorate",
    "Dr. jur.": "Doctorate",
    "L.S.A.": "Professional",
    "L.R.C.P.": "Professional",
    "M.R.C.S.": "Professional",
}

FREE_TEXT_NOTES = {}

_NOT_CLOSED = set()


def canonical_set(field):
    return {t for group in FIELDS[field]["groups"].values() for t in group}


def known_vocab():
    """Closed facet lists. Phrases are not included."""
    return {f: canonical_set(f) for f in FIELDS if f not in _NOT_CLOSED}


def field_synonym_maps():
    return {f: {k.lower(): v for k, v in FIELDS[f]["synonyms"].items()}
            for f in FIELDS if FIELDS[f]["synonyms"]}


def field_parent_maps():
    """Kept so older callers find no parent links."""
    return {}


def self_check():
    problems = []
    for field, spec in FIELDS.items():
        canon = canonical_set(field)
        if len(canon) != len(list(t for g in spec["groups"].values() for t in g)):
            problems.append(f"{field}: duplicate facet")
        for variant, target in spec["synonyms"].items():
            if target not in canon:
                problems.append(f"{field}: synonym '{variant}' targets '{target}'")
    for phrase, level in DEGREE_LEVEL.items():
        if level not in canonical_set("Education_Degree_Facet"):
            problems.append(f"DEGREE_LEVEL {phrase} -> {level} is not a facet")
    return problems


if __name__ == "__main__":
    issues = self_check()
    print(f"{len(FIELDS)} facet fields | "
          f"{sum(len(canonical_set(f)) for f in FIELDS)} facets | "
          f"{len(DEGREE_SPELLING)} degree spellings")
    if issues:
        print("\nPROBLEMS:")
        for p in issues:
            print("  -", p)
    else:
        print("self-check: OK")
