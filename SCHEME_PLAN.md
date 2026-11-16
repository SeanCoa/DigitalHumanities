# Catalog scheme

This is the plan for replacing closed category lists with a phrase plus a facet. Volumes 1 and 2 are test extracts. They will be run through this pipeline again and reviewed again. The map does not need to keep their current columns.

The Westfall catalog is a separate project and is left as it is.

Two lists from the Dictionary’s index of volumes 1–16 are scraped before the prompt is rewritten. **Societies Indexed** supplies the society names and the spellings the Dictionary treats as one body. **List of Scientists by Field** supplies the field headings the editors use. Both are name lists. A page reference in either list is not evidence about a person, and the roster of scientists under a field is not copied into the catalog. Periodicals Indexed and the name index are not scraped.

## The record and the index

Two different things are stored.

The **phrase** is the entry’s own short label: `saddler`, `Dr. phil.`, `Privatdozent`, `Lutheran`, `University of Padua`. It is a few words, not a sentence. The quotation that justifies it stays in the evidence file. The public catalog does not carry that quotation.

The **facet** is a closed grouping used as a filter: church, craft, learning, Protestant, university. Occupation, religion, means, patronage, and school-type facets live in `scripts/vocab.py`. Discipline facets are the scraped field headings, and society aliases are the scraped society list. A phrase may have a blank facet. A blank facet is a finished state. It is how a word the list does not name, including most non-European arrangements, stays filterable by search without being forced onto a European parent.

The facet never replaces the phrase. The map has two controls. The facet dropdown is small and stable. A second control searches the phrases that actually occur. Choosing learning returns every academic post. Choosing `Privatdozent` returns that post. A person panel shows the phrase. The facet is labeled as the grouping.

`scripts/vocab.py` stops being a list of allowed specifics. Synonym tables that rewrite a named office (`vicar` to `Priest`, `latitudinarian` to `Anglican`, `industrialist` to `Merchant`) are removed. What remains are the facet lists, plus spelling aliases for one and the same thing (`B.Sc.` with `B.S.`). Anything beyond spelling is a reviewed dictionary entry, not a silent fold.

## What changes in the pipeline

Extraction still reads each entry twice at temperature 0, and a fact still has to survive the quotation check. The comparison rule changes for phrases, because two honest readings will not use the same wording.

Closed values still use a strict majority. A tie is written as the sentinel. This covers gender, the wealth flag, certainty flags, facets, the country convention, and sentinels.

A phrase is kept by a different rule:

- Compare the two runs after ignoring case and accents.
- If they match, keep the phrase when it is a contiguous span in the entry.
- If they differ and only one is a span in the entry, keep that one.
- If both are spans in the entry and they are not the same span, write the sentinel and send both candidates to review. Do not keep run 1, and do not keep the longer phrase.
- A phrase that is not in the entry is dropped even when both runs produced it.

The quotation has to be the deciding sentence, not a nearby one. A medal sentence that mentions the Royal Society does not support membership. An occupation sentence does not support the wealth flag.

Review stays in two passes. The person pass accepts dates, places, coordinates, and any phrase the two runs did not settle. The dictionary pass accepts a new phrase once: its facet, and any alias to a phrase already accepted. The key is the field, the phrase, and the sense. `Minister` is not accepted once for every later use. `Vicar` can be, because the word has one sense. A later volume inherits the entry. A bad entry can be undone and reapplied without a new extraction.

Export still refuses a validation report that was never loaded. An unaccepted flagged cell is written as `Unknown` or `None`. The file is marked `reviewed`. The map build requires that column on every row.

## Fields

### Names, gender, and who the row is about

The heading is a person. A see-reference, a half-title, an empty article, and an article whose subject is a theory, a book, or an institution are skipped. A joint life of two people becomes two rows only when the entry gives each person a separable account. Otherwise the row is the person named in the heading, and the other person stays in the notes.

Name rules stay. A heading that is not a Western forename plus surname stays whole in `Last_Name`, with `First_Name` as `None`. `Alt_Name` is only a string that appears in the heading, the body, or the sources. Particles stay on the surname form the entry uses.

Gender is taken from a pronoun, title, or kinship word that refers to the subject. The father’s “he” in the opening sentence does not gender the subject. A given name does not either. No such word means `Unknown`.

### Dates

A year is a numeral the entry prints for that event. `ca.` and `?` keep the year and set certainty to `0`. A century, a floruit, and “after” or “before” a year are `Unknown`. A season or a feast day, with no numeral, does not become a month or a day.

When the entry prints a non-Christian year and an A.D. equivalent, the A.D. numeral is the year. When it prints only a Hijri year, a regnal year, or a dual Old Style/New Style date with no A.D. equivalent, the year is `Unknown` and the printed form goes in `Compiler_Details_Notes`. A positive year always means A.D. A B.C. year is negative.

A stated age is not used to invent the missing year. If both years are printed and they contradict the entry, or each other (death before birth, or a stated age that disagrees by more than a year), both years and the lifespan become `Unknown`. A lifespan under 20 or over 105 is kept when both numerals are printed and the entry does not contradict them. Certainty on those rows is `0`. The old automatic blanking of an unusual span is dropped, because it deletes printed years that may be true.

### Places

The town is the entry’s name, including a one- or two-letter repair when the sentence already identifies that place. A later name is not substituted. `Falmouth` stays `Falmouth` when the entry says Falmouth.

The country column stays a convention, not a phrase. England, Scotland, Wales, and Ireland are used, not United Kingdom. A place in a historic German or Italian state is `Germany` or `Italy`, unless the entry’s point is the historic state itself. A region is recorded only when the entry states it.

An alternative birthplace (“Strasbourg or Paris”) does not pick a winner. The town is `Unknown`, and both names go in the notes. “At sea,” exile, and a hospital are recorded only when the entry gives them as the place of the event.

`HISTORICAL_PLACE_OVERRIDES` is used only to build the geocode query. Danzig may be queried as Gdańsk so a gazetteer can find it. The stored town remains Danzig. The stored country is not rewritten to the modern country of the query. If the winning cluster falls in a different country from the stored country, the coordinate is `Unknown` and the row is reviewed.

Geocoding is unchanged in structure. The town is queried. A missing town is not filled from the country or from an institution’s known seat. One source is not a coordinate. A strict majority of the returned points must lie within 40 km. Anything weaker stays `Unknown` until a reviewer chooses a point. Birth, death, and education use the same rule.

### Father

`Father_Occupation` holds one phrase: the living the entry treats as the father’s. A second trade goes in `Father_Notes`, not a second code. The facet is the closed grouping, or blank.

The mother’s occupation, and a guardian’s, do not go in this field. They stay in the note. They count as the father’s only when the entry says that person was the father.

`Father_Is_Wealthy` follows an economic word said of the father or the household. Wealthy, prosperous, a fortune, or well-to-do is `1`. Poor, humble, or of modest means is `0`. No such word is `-1`. A title, “of good family,” and the occupation itself do not set the flag. A merchant is not thereby rich, and a laborer is not thereby poor.

A relative who paid for schooling is family. That person is a patron only when the entry describes support of the work, not of the upbringing.

### Education

A stop exists when the entry describes this person as a student at a teaching body. The deciding words are studied, attended, enrolled, pupil, trained, or took a degree there. The teaching is done by a body, named or clearly implied. A father, a private tutor, or a master in a shop goes in `Education_Notes`. So does an apprenticeship in that shop, self-study, and a journey that was not enrollment.

These also do not create a stop: attending public lectures with no enrollment, a degree earned only by examination, a degree the entry says was bought, and a course the person failed to complete unless the entry still says they studied at that body. Walking a hospital’s wards counts when the entry presents it as training. A military academy counts when the person was a student there. Correspondence with a professor does not.

`Not Specified` is only for study in a named place with no school named. Every such stop is reviewed. A school the dictionary has not seen is reviewed once. A school already accepted, with the same kind of sentence, passes.

The stop stays one aligned record:

`Institution @ City, Region, Country, Start_Year, End_Year, Degree`

The degree slot on the stop is filled only when the entry says the diploma was conferred there. A diploma with no place stays in `Education_Degrees` and is not pinned to the nearest university. An external degree does not invent a stop.

The institution phrase is the entry’s name. The forced English forms, including the rule that Basel, Edinburgh, Göttingen, Heidelberg, Leipzig, St. Andrews, Tübingen, and Uppsala must be written `University of X`, are dropped at extraction. So is the rule that a faculty or college is deleted in favor of the parent university. Sheffield Scientific School stays Sheffield Scientific School. The dictionary may later alias two names of one school. It does not do that during extraction.

The facet is the kind of school: university, college, medical school, school, seminary, academy. It may be blank. The map dot is keyed by the phrase plus the town, so Trinity College in Cambridge and Trinity College in Dublin stay two places. The dot is labeled with the phrase, not the facet.

A missing town stays `Unknown`. The dictionary is not a gazetteer. University of Michigan does not become Ann Arbor unless the entry says Ann Arbor. If a school moved, each stop keeps the town the entry gives for that period. Coordinates are not copied from the other period.

### Degrees

The phrase is the diploma the entry names. Spelling aliases cover one diploma (`B.Sc.` with `B.S.`, `A.B.` with `B.A.`). A different national diploma stays different. `Dr. phil.` is not a Ph.D. A historical `Laurea` is not a bachelor’s. `Ch.B.` is not `M.B.`

The level facet is filled only when the diploma’s name states the level, or a reviewer has accepted that diploma once. Otherwise the facet is blank.

A school-leaving certificate, a Habilitation, and an agrégation are not degrees.

`None` and `Unknown` are split. `None` means the entry says there was no degree. `Unknown` means the entry does not say. The same split is used for instruments, societies, and patronage. The old conflation was a property of the test catalogs and is not kept.

### Religion

The phrase is the entry’s word for this person’s own affiliation. Certainty `1` requires the entry to state that affiliation: a named faith, a conversion, a baptism, or admission to a church. The parents’ religion is not the person’s. “Of Jewish descent” is ancestry, recorded in the note, and it is not `Jewish` unless the entry says they belonged to the community.

A role such as monk or parish priest may be recorded at certainty `0`, and only as far as the role itself goes. A parish priest in England is not thereby Anglican. Region, century, and fame leave the field `Unknown`.

`Christian`, `Evangelical`, and `Reformed` are kept when the entry uses them. Their facet stays blank, so they do not enter the Protestant filter. Quaker, Unitarian, and Anabaptist are not given the Protestant facet.

A conversion keeps every affiliation the entry states, in the order stated. A faith the entry does not name is not invented as the “before.” A person who moved from one faith to another matches both facets. The panel shows the order.

Freemasonry and similar bodies are not religions unless the entry presents them as the person’s faith. They are not societies either unless the membership test below is met. Otherwise they stay in the notes.

### Disciplines

The phrase list is every sustained focus the body describes, in the entry’s words. There is no parent requirement and no cap of one subfield. A passing mention is omitted. The field the person is famous for is omitted when the body does not describe it.

The closed facet list is the field headings from List of Scientists by Field, not a list inferred from the first two volumes. The header line of an entry is matched to that list and stored apart, marked as the editor’s classification. A header term that matches a scraped heading becomes the facet. A header term that matches none is kept for review and does not get forced onto a nearby field. `Physics` on the header of a natural philosopher does not become the person’s phrase. A person listed under a field in the index does not receive that field unless their own entry supports it.

`Algology` is not rewritten to `Phycology` during extraction. If they are the same focus, a reviewer aliases them once. Until then both phrases exist. The scraped field list does not merge them on its own.

### Means of support

A means is a living the entry shows they held: a post, a practice, a salary, a trade, an estate, a pension they lived on, or regular support. The phrase is that living. The facet is the closed livelihood, or blank. Several sustained livings are kept, in the order the entry presents them.

These are not means: a discipline they are remembered for, a single payment, a prize, a stipend held only while a student, and a retirement that names no income. Silence is `Unknown`. `None` is not used. Someone enslaved, conscripted, or imprisoned is recorded with the entry’s phrase and a blank facet, not with a trade the list happens to contain.

A paid office is a means. It is also patronage only when a person or body is identified as the supporter. A parish priest’s living is church life. It is not automatically a patron, and it is not the father’s occupation.

### Patronage

A patron is a person or body the entry says supported, housed, paid, or advanced the work. The phrase is that patron’s name or office. One described patron is one facet. A court appointment is not also filed as a government patron unless the entry describes a second patron.

A salaried post already counted as a means is not, by itself, a patron. A dedication alone goes in the note. It becomes patronage when the entry also says the dedicatee supported them. `None` means the entry says they had no patron. Silence is `Unknown`.

### Instruments

An instrument is a named device the entry says the person invented, built, designed, or materially improved. Using one, owning one, and being associated with a laboratory full of them do not qualify. A unit, a method, a book, and an experiment do not. A device named after the person counts only when the entry says they devised it.

The phrase is the entry’s singular name. Aliases are reviewed, not folded in the prompt. `None` means the entry associates no device. `Unknown` means the entry never discusses instruments.

### Societies

Membership requires a membership verb in the deciding sentence: elected, admitted, fellow, member, or a named class such as corresponding, honorary, or foreign member. The phrase is the society’s proper name. “Numerous societies” is not a value.

A medal, a prize, a lecture, a paper read to the society, a visit, and an invitation that was declined do not count. The award stays in `Scientific_Societies_Notes`. Expulsion or resignation still counts as membership, and the note records the ending.

A paid secretaryship of an academy is membership and a means, each from its own sentence. An informal circle or a correspondence network stays in the notes.

Society names are filtered as phrases. They do not need a facet. Societies Indexed is the seed of the alias dictionary: the names it treats as one body are one society before the first volume is reviewed. The phrase stored on a person is still the spelling in that person’s entry. The alias only joins spellings for the filter. A society named in an entry and absent from the index is a new phrase and waits for review. It is not dropped. A society present in the index is not added to anyone. Membership still requires the membership sentence in the entry. The merge is per field, so the same name used as a patron does not become a membership.

## One sentence, several fields

A single sentence about a parish priest can tempt religion, means, patronage, and the father’s occupation. Each field still has to pass its own test. Passing one does not fill the others.

## Dictionary

The dictionary is seeded from the two scraped lists, then extended by review. Societies Indexed is loaded first, as aliases among society names. List of Scientists by Field is loaded as the closed discipline-facet list, headings only. The review tool writes later rows into the same file, and the next extraction reads it. Each accepted row records the field, the phrase, the sense, the facet (or blank), and any alias. Ambiguous phrases are stored with the sense, and they are not applied to a sentence that does not show that sense.

The dictionary does not supply a city, a year, a faith, or a wealth flag. It does not decide that a stop deserved to exist. Worthiness is decided per row from the deciding sentence. The dictionary only recognizes a phrase that has already qualified.

## Review and the map

The person review loads the extraction, the raw volume, and the validation report. The report now includes ties, grounding failures, and phrases the two runs left unsettled. Export writes `reviewed` and withholds anything unaccepted.

The dictionary review is a table of new phrases: the phrase, the field, the count, one deciding quotation, the suggested facet, and accept or change. A society already seeded by Societies Indexed is not in that table unless the entry’s spelling is new. A discipline facet is suggested only from the scraped field headings. Societies that the index does not contain, and instruments, will mostly be unique. That pass does not get shorter for them. Occupation, religion, means, and degrees get shorter after the first volume, because the words repeat.

Coordinates are reviewed separately. Accepting an alias does not accept a point.

The map reads the new columns only. Facet filters use the facet columns. Phrase search uses the phrase columns. Institution names are not split on commas. Education dots stay one per institution-at-a-town, fanned out when several schools share a coordinate. `Verification` must be `reviewed` on every row. There is no legacy path.

## What is built, in order

1. Scrape Societies Indexed and List of Scientists by Field. Keep each society name and any variant the index prints as the same body. Keep field headings, and the grouping of those headings if the index has one. Leave out the roster of people under each field, and do not treat a page number as a fact.
2. Load those scrapes into the dictionary seed: society aliases from the first, discipline facets from the second. Write nothing into a person’s row.
3. Rewrite `prompt/prompt_template.md` to the rules in this plan, and rebuild `prompt/prompt.md`. The discipline appendix is the scraped field list. The other facet lists are the ones this plan keeps. Spelling aliases stay. Specific-term lists do not.
4. Change `scripts/vocab.py` to match. Remove the specific-term lists and the synonym folds.
5. Add the phrase and facet columns in `scripts/extract.py`. Change agreement so free-text phrases follow the span rule above. Extend `scripts/ground.py` so each field checks its deciding sentence. Match a header discipline to the scraped field list, and match a society spelling to the seeded aliases, without adding either from the index alone.
6. Add the dictionary pass in the review tool. Keep the person-export gate.
7. Keep geocode overrides on the query only, and reject a cluster whose country contradicts the stored country.
8. Update the map filters and the education key (phrase plus town). Require `Verification`. Discipline facets on the map are the scraped field headings.
9. Update `README.md` to this scheme.
10. Re-extract and review. Do not convert the current CSVs in place. Their notes are paraphrases, and their evidence file is not a set of quotations.

## Out of scope

The Westfall catalog, its vocabulary, and its map are not part of this scheme. The volume scrape is unchanged. Periodicals Indexed and the name index are not inputs. The model is unchanged. No volume is extracted as part of writing this plan.
