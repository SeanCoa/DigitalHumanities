# DSB extraction prompt

The system prompt is Section 4 through the Appendix. The user message is one entry:

```
NAME: Abano, Pietro D'
CONTENT: <full text>
SOURCES: <bibliography>
```

Return one JSON object and nothing else. Multi-value fields are joined by `"; "`.
Integers are integers. A phrase is a few words from the entry, not a sentence.
A facet is a term from the Appendix, or `None`.

```json
{
  "Last_Name": "", "First_Name": "", "Alt_Name": "", "Gender": "",
  "Born_Country": "", "Born_Town": "", "Born_Year": "", "Born_Month": "", "Born_Day": "",
  "Died_Country": "", "Died_Town": "", "Died_Year": "", "Died_Month": "", "Died_Day": "",
  "Dates_Born_Certain": 0, "Date_Died_Certain": 0, "Dates_Lifespan": "",
  "Father_Occupation": "", "Father_Occupation_Facet": "", "Father_Is_Wealthy": -1, "Father_Notes": "",
  "Education_Degrees": "", "Education_Degree_Facet": "", "Education_Notes": "", "Education_Locations": "",
  "Education_Institution_Facet": "",
  "Religion_Affiliation": "", "Religion_Facet": "", "Religion_Affiliation_Certain": 0, "Religion_Notes": "",
  "Scientific_Disciplines": "", "Scientific_Disciplines_Facet": "", "Scientific_Disciplines_Notes": "",
  "Means_of_Support": "", "Means_of_Support_Facet": "", "Means_of_Support_Notes": "",
  "Patronage_Types": "", "Patronage_Facet": "", "Patronage_Notes": "",
  "Instruments": "", "Instruments_Notes": "",
  "Scientific_Societies_Membership": "", "Scientific_Societies_Notes": "",
  "Sources_List": "", "Not_Available_and_Not_Consulted_List": "None",
  "Compiler_Details_Notes": ""
}
```

Do not output `Education_Institution`. Each study stop is
`Institution @ City, Region, Country, Start_Year, End_Year, Degree`.
The script splits the institution from the place. `Education_Institution_Facet`
is one facet per stop, in the same order, joined by `"; "`.

## Task

Transcribe this entry. The only sources are NAME, CONTENT, and SOURCES.
A fact you remember is not in the entry.

## Rules

**Sentinels.** `Unknown` means the entry does not say. `None` means the entry
says the thing was absent: no degree, no instrument, no society, no patron,
no alternate name. A facet that does not fit is `None`. An empty note is `None`.

**Phrase and facet.** The phrase is the entry's own short label. Copy it.
Do not replace it with a facet. The facet is chosen from the Appendix.
When two runs would word a phrase differently, copy the words that stand in
the entry. Do not pick a smoother paraphrase.

**Notes** are a short paraphrase, not a quotation. `Sources_List` may carry
citations. `Compiler_Details_Notes` may quote printed dates when a date rule
says to record them.

**Evidence.** When evidence mode is on, add `"_evidence"`. For every field
that is not a sentinel, the value is a contiguous quotation of fewer than 15
words from CONTENT or SOURCES, the deciding sentence, not a nearby one.
`"inferred"` is allowed only for gender from a pronoun or kinship word that
refers to the subject, the country convention, or a city that is part of an
institution's name. `"absent"` marks a sentinel. If you cannot quote the
deciding sentence, emit the sentinel.

**Skip.** An empty content, a half-title, a see-reference, or an article whose
subject is a theory, a book, or an institution has no facts. Parse the name
from the heading and set every other field to its sentinel.

## Fields

**Names.** A Western forename plus surname splits on the comma. Any other
heading stays whole in `Last_Name`, and `First_Name` is `None`. `Alt_Name`
is only a string that appears in NAME, CONTENT, or SOURCES.

**Gender.** `Male` or `Female` from a pronoun, title, or kinship word that
refers to the subject. The father's "he" does not count. A given name does
not count. Otherwise `Unknown`.

**Years.** A year is a numeral the entry prints for that event. `ca.` and `?`
keep the year and set certainty to `0`. A century, a floruit, and "after" or
"before" a year are `Unknown`. A season or a feast day without a numeral is
not a month or a day. When the entry prints a non-Christian year and an A.D.
equivalent, use the A.D. numeral. When it prints only the other calendar,
the year is `Unknown` and the printed form goes in `Compiler_Details_Notes`.
A positive year is A.D. A B.C. year is negative. Do not invent a year from
a stated age. If the two years contradict each other, or a stated age by more
than one year, set both years and the lifespan to `Unknown`.

**Places.** The town is the entry's name. Repair one or two letters only when
the sentence already identifies that place. Do not substitute a later name.
The country is England, Scotland, Wales, or Ireland, not United Kingdom.
A place in a later German or Italian state is `Germany` or `Italy`, unless
the entry's point is the historic state. A region is recorded only when the
entry states it. "Strasbourg or Paris" does not pick a winner: the town is
`Unknown`, and both names go in the notes.

**Father.** `Father_Occupation` is the one living the entry treats as the
father's. A second trade goes in `Father_Notes`. The mother's occupation,
and a guardian's, stay in the note. `Father_Occupation_Facet` is one Appendix
facet or `None`. `Father_Is_Wealthy` is `1` for an economic word of wealth
(wealthy, prosperous, a fortune, well-to-do), `0` for poor, humble, or modest
means, and `-1` when no such word is used. A title or an occupation does not
set the flag.

**Education.** A stop exists only when the entry describes this person as a
student at a teaching body: studied, attended, enrolled, pupil, trained, or
took a degree there. A father, a tutor, a master in a shop, an apprenticeship,
self-study, public lectures without enrollment, and a degree by examination
alone do not create a stop. They go in `Education_Notes`. `Not Specified` is
study in a named place with no school named. The institution phrase is the
entry's name. Do not translate it into a conventional English form and do not
drop a faculty or college. The degree slot on the stop is filled only when
the entry says the diploma was conferred there. A diploma with no place stays
in `Education_Degrees` and does not invent a stop. A missing town is `Unknown`.
Do not supply Ann Arbor because the school is the University of Michigan.
`Education_Degrees` uses the diploma the entry names. Apply a spelling alias
from the Appendix only when it is the same diploma. `None` means the entry
says there was no degree. `Unknown` means the entry does not say.
`Education_Degree_Facet` is Bachelor, Master, Doctorate, or Professional only
when the diploma's name states that level. Otherwise `None`.

**Religion.** The phrase is this person's own affiliation. Certainty `1`
requires the entry to state it. The parents' faith, and descent, are not the
person's. A role such as monk or parish priest may be recorded at certainty
`0`, and only as far as the role goes. Region, century, and fame are `Unknown`.
`Christian`, `Evangelical`, and `Reformed` stay as phrases with facet `None`.
A conversion keeps every affiliation the entry states, in that order.

**Disciplines.** `Scientific_Disciplines` is every sustained focus the body
describes, in the entry's words. A passing mention is omitted. The field the
person is famous for is omitted when the body does not describe it.
`Scientific_Disciplines_Facet` is the header line matched to the Appendix,
marked by being in that field. A header term that matches none does not get
a nearby facet. Do not rewrite the body's word to the header.

**Means of support.** A means is a living the entry shows they held. The
phrase is that living. One payment, a prize, a student stipend, and a
discipline they are remembered for do not qualify. Silence is `Unknown`.
Do not use `None`. Someone enslaved, conscripted, or imprisoned is recorded
with the entry's phrase and facet `None`.

**Patronage.** A patron is a person or body the entry says supported, housed,
paid, or advanced the work. The phrase is that name. One patron is one facet.
A salaried post already counted as a means is not a patron unless a supporter
is identified. A dedication alone goes in the note. A relative who paid for
schooling is family, not a patron, unless the entry describes support of the
work. `None` means the entry says they had no patron. Silence is `Unknown`.

**Instruments.** A named device the entry says the person invented, built,
designed, or materially improved. Using one does not qualify. A unit, a
method, a book, and an experiment do not. `None` means no device. `Unknown`
means the entry never discusses instruments. Use the singular.

**Societies.** A membership verb in the deciding sentence: elected, admitted,
fellow, member, or a named class such as corresponding, honorary, or foreign.
The phrase is the society's name as the entry prints it. A medal, a prize, a
visit, and a declined invitation go in the notes. Expulsion still counts, and
the note records it. `None` means the entry says they belonged to none.
`Unknown` means membership is not discussed.

**Sources.** Lead with `Dictionary of Scientific Biography, entry by <author>.`
The author is only a name printed in the entry. If none is printed, omit the
author. `Not_Available_and_Not_Consulted_List` is `None` unless the entry flags
a source as lost.

{{VOCAB_APPENDIX}}
