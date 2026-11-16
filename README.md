# Dictionary of Scientific Biography catalog

This project turns the *Dictionary of Scientific Biography* (DSB) into a structured prosopography of scientists and a map of where they were born, studied, and died.

Each Gale entry is scraped, extracted into a fixed schema, reviewed by hand, geocoded, and appended to a running database. The schema follows Richard S. Westfall’s *Catalog of the Scientific Community*. Westfall’s own catalog is kept here as a separate map, so the new DSB catalog can be compared with that earlier census of the scientific revolution.

The DSB catalog files use the `cdsb_` prefix. Westfall’s catalog uses the `westfall_` prefix.

## Where the work stands

The original DSB is 16 volumes plus supplements. Volumes 1–6 are scraped. Volumes 1–2 have been extracted. Volume 2 has been reviewed and geocoded. Volume 1 has coordinates filled in, and its review file is still marked in progress.

| Volume | Entries | Span | State |
| --- | --- | --- | --- |
| 1 | 367 | Abailard → Berg | Extracted. Review file is `databases/vol01_review_inprogress.csv`. No reviewed or geocoded copy in `archive/`. |
| 2 | 346 | Berger → just before Cabanis | Finished through geocoding. `databases/vol02_review_finished.csv` matches `archive/dsb_geocoded_reviewed_vol02.csv`. |
| 3 | 369 | Cabanis → Dechen | Scraped only |
| 4 | 311 | Dedekind → Firmicus Maternus | Scraped only |
| 5 | 359 | Fischer → Haber | Scraped only |
| 6 | 307 | Hachette → H | Scraped only |

`visualizations/cdsb_database.csv` is the map database: volumes 1 and 2 combined (712 rows). The Westfall map covers 630 people born between 1471 and 1680.

Volume 2’s raw file includes a half-title page that is not a biography. Volume 6 has one too. The extractor logs those in the failures report.

## Layout

Scripts find these folders from the project root, so they can be run from any working directory.

```
raw_volumes/     Scraped DSB text. One CSV per volume: Name, Content, Sources.
scripts/         Pipeline code. vocab.py is the controlled vocabulary.
prompt/          Extraction prompt. prompt_template.md plus vocab.py builds prompt.md.
review_tools/    Browser tools for extraction review, geocode review, and spot checks.
archive/         Frozen products of each volume, named with _volXX.
databases/       The running catalog after review and geocoding.
visualizations/  The two maps and the scripts that rebuild them.
```

A run also drops working files in the project root (`dsb_extracted.csv`, `dsb_evidence.csv`, `dsb_agreement.txt`, `dsb_validation.txt`, `dsb_new_vocab.txt`, `dsb_failures.txt`, `dsb_geocoded.csv`). The scripts then copy the ones worth keeping into `archive/` with the volume tag. The root copies are the files you load into the review tools.

## What one row records

One person is one row. Closed fields use a controlled vocabulary. `Unknown` means the entry does not say. `None` means the person had none of that thing, or a notes field is empty. Multi-value fields join values with `"; "`.

The extraction writes these fields:

- Name: `Last_Name`, `First_Name`, `Alt_Name`, `Gender`
- Birth and death: country, town, year, month, day, plus `Dates_Born_Certain`, `Date_Died_Certain`, and `Dates_Lifespan`
- Family: `Father_Occupation`, `Father_Is_Wealthy`, `Father_Notes`
- Education: `Education_Institution`, `Education_Degrees`, `Education_Notes`, `Education_Locations` (each stop is `Institution @ City, Region, Country, Start_Year, End_Year` in the prompt, then split downstream)
- Religion, scientific disciplines, means of support, and patronage, each with a notes column
- `Instruments` and scientific-society memberships, each with a notes column
- `Sources_List`, `Not_Available_and_Not_Consulted_List`, `Compiler_Details_Notes`

Agreement mode adds `Disagreement_Count` and `Inconsistent_Fields`. Geocoding adds `Born_Latitude`, `Born_Longitude`, `Died_Latitude`, `Died_Longitude`, and `Education_Coordinates`.

`scripts/vocab.py` is the only place to add a canonical term or a synonym. These fields have vocabularies there: gender, father’s occupation, religion, discipline, means of support, patronage, and degree. Some ambiguous words are deliberately left unmapped (for example chemist, minister, captain) so a person can review them instead of the validator silently rewriting them.

The Westfall catalog uses the same shape, with `Technological_Involvement_Type` and `Technological_Involvement_Notes` where the DSB catalog uses instruments.

## Processing a volume

Set `VOLUME` in the script you are about to run, and point that script’s input at this volume’s file. Paths below are from the project root.

1. **Scrape.** In `scripts/scraper.py`, set `VOLUME` and `doc_ids`, then run it. You log in to the Notre Dame Gale proxy in the browser window that opens. The CSV is written to `raw_volumes/raw_volXX.csv`. If that file already exists, the script stops. Pass `--overwrite` only when you mean to replace it. The checked-in `doc_ids` range is a 35-id test span, not a full volume.

2. **Extract.** In `scripts/extract.py`, set `INPUT_CSV` and `VOLUME`. The default input is `raw_volumes/raw_vol01.csv`. Run `python scripts/extract.py`. This needs `ANTHROPIC_API_KEY`. The default mode is `agreement`: each entry is extracted twice with Claude Haiku at temperature 0. A field is kept only when the runs agree and the quoted words are in the entry. A tie is written as `Unknown` or `None`. A quotation that is not in the entry clears that fact, and the note that described it. Evidence is written separately. `dsb_validation.txt` lists both the disagreements and the facts that were cleared. Results are copied into `archive/` as `dsb_extracted_volXX.csv`, `dsb_evidence_volXX.csv`, `dsb_agreement_volXX.txt`, `dsb_validation_volXX.txt`, `dsb_new_vocab_volXX.txt`, `dsb_failures_volXX.txt`, and `prompt_volXX.md`. A command-line volume, such as `python scripts/extract.py 3`, changes the archive tag only. The input path stays whatever `INPUT_CSV` is set to. `python scripts/verify.py extracted.csv raw_vol.csv evidence.csv -o gated.csv` repeats the quotation check on a finished extraction without calling the model.

3. **Read the failures report** (`dsb_failures.txt` in the project root) and fill or rerun any entry that did not parse.

4. **Export the vocabulary** with `python scripts/export_vocab.py`. It writes `prompt/vocab.json` for the review tool.

5. **Review the extraction.** Open `review_tools/review_tool.html` and load `dsb_extracted.csv`, the raw volume, `dsb_validation.txt`, and `prompt/vocab.json`. The validation report is required. Export writes `extracted_reviewed.csv` with a `Verification` value of `reviewed`. A flagged cell that you have not edited or chosen is written as `Unknown` or `None`. Also export `dsb_vocab_additions.json`.

6. **Archive the review** as `archive/dsb_extracted_reviewed_volXX.csv`.

7. **Fold in new terms.** Put `dsb_vocab_additions.json` in `scripts/` and run `python scripts/apply_additions.py`. It edits `scripts/vocab.py`, checks that the vocabulary is still consistent, and deletes the additions file. Then run `python scripts/build_prompt.py` so `prompt/prompt.md` picks up the new terms. Do not hand-edit the generated appendix in `prompt.md`. Edit `vocab.py` or `prompt/prompt_template.md`, then rebuild.

8. **Check the shape of the reviewed file.** `python scripts/validate.py` checks `databases/vol01_review_inprogress.csv` by default. Pass another CSV, and optionally a report path, on the command line. `SHOW_DISAGREEMENTS` is `False`, so this pass checks dates, codes, synonyms, and duplicate tokens, and does not re-list disagreements you already resolved. It does not decide whether a fact is true. That decision is the quotation check in step 2 and the review export in step 5.

9. **Geocode.** In `scripts/geocoder.py`, set `VOLUME`. The input is `archive/dsb_extracted_reviewed_volXX.csv`. The script writes `dsb_geocoded.csv` in the project root and copies it to `archive/dsb_geocoded_volXX.csv`, with a log at `archive/dsb_geocode_volXX.txt`. If the archived geocode already exists, the script stops. Pass `--overwrite` only when you mean to replace that volume. A stop whose town is `Unknown` is left `Unknown` and is not geocoded from the country alone. Volume 2’s reviewed coordinates are in `archive/dsb_geocoded_reviewed_vol02.csv`. `archive/westfall_geocoded.csv` and `archive/westfall_geocode.txt` are a geocode of the Westfall catalog; they used to be filed under the volume 2 names.

10. **Review coordinates.** Open `review_tools/review_geocode.html`, load the geocoded CSV, and upload `review_tools/geocode_memory_file.json` so earlier place decisions carry forward. Export `geocoded_reviewed.csv` and a new memory file. A coordinate is written into the birth, death, and education columns only when every returned source agrees, or when you choose a point yourself. A single source or a partial cluster is left `Unknown`. Keep the new memory file for the next volume.

11. **Archive the reviewed geocode** as `archive/dsb_geocoded_reviewed_volXX.csv`. Clean it up and add it to `databases/`.

12. **Update the map.** Add the new rows to `visualizations/cdsb_database.csv`, then run `python visualizations/cdsb_build.py`. That rewrites `cdsb_data.json` and `cdsb_visualization.html`. If the catalog has a `Verification` column, every row must say `reviewed` or the build stops. The current map file has no such column; it was built before this gate.

`review_tools/random_review.html` is a separate spot-check tool. Load a database CSV and the matching raw volume, sample rows, and mark anything that looks wrong.

## Geocoding

Each birth place, death place, and education stop is queried against several sources: Nominatim, GeoNames, Mapbox, MapTiler, Wikipedia, Wikidata, and Pleiades. Points that fall within 40 km of each other are treated as the same place. The coordinate is kept only when a strict majority of the sources that returned a point fall in that cluster. One source, or a split result, is left `Unknown`. Pleiades is skipped when the year is after 700, because its coverage is ancient and late antique.

Historical names that would send a modern geocoder to the wrong country (Danzig, Christiania, and similar cases) are rewritten in `HISTORICAL_PLACE_OVERRIDES` inside `scripts/geocoder.py` before the query is built. GeoNames, Mapbox, and MapTiler credentials are set in that same file.

## Maps

Both dashboards are MapLibre pages on an OpenHistoricalMap basemap, with a time slider, filters, a data table, and life paths from birth through education to death. MapTiler is the fallback basemap in the build scripts.

- `visualizations/cdsb_visualization.html` is the DSB catalog.
- `visualizations/westfall_visualization.html` is Westfall’s catalog. Rebuild it with `python visualizations/westfall_build.py` after editing `westfall_database.csv`.

Open the HTML file in a browser. The basemap needs a network connection. The date-filter plugin is vendored at `visualizations/vendor/maplibre-gl-dates.js` because the rebuild inlines it. `cdsb_data.json` and `westfall_data.json` are the data layers the builders write. The HTML pages already contain that data, so the JSON files are for inspection and for rebuilding.

## Archive names

| File | What it is |
| --- | --- |
| `dsb_extracted_volXX.csv` | Model output, before review |
| `dsb_evidence_volXX.csv` | Short justification for each extracted cell |
| `dsb_agreement_volXX.txt` | Fields where the two extraction passes disagreed, with both values |
| `dsb_validation_volXX.txt` | Mechanical checks plus those disagreements |
| `dsb_failures_volXX.txt` | Entries that did not return a usable row |
| `dsb_new_vocab_volXX.txt` | Terms the model used that were not yet canonical |
| `prompt_volXX.md` | The exact prompt used for that volume |
| `dsb_extracted_reviewed_volXX.csv` | After human review, before geocoding |
| `dsb_geocoded_volXX.csv` | Geocoder output, with one column per source |
| `dsb_geocode_volXX.txt` | Geocoder log: failures, places with no consensus, and partial disagreements |
| `dsb_geocoded_reviewed_volXX.csv` | After coordinate review. One latitude and longitude per place |

`prompt/prompt.md` currently matches `archive/prompt_vol01.md`. Volume 2’s archived prompt is slightly different. Rebuilding the prompt from `vocab.py` would also change the degree list, because some former synonyms are now canonical terms. Run `build_prompt.py` when you want the live prompt to catch up, not before.

## Requirements

Python packages used by the pipeline:

- `anthropic` for extraction
- `pandas` for the scraper, geocoder, and map builders
- `selenium` and `beautifulsoup4` for the scraper
- `requests`, `certifi`, and `geopy` for geocoding
- `pleiades_search_api` for the Pleiades source (`pip install git+https://github.com/isawnyu/pleiades_search_api.git`)
- `pycountry` is optional. Country codes fall back to a table in the geocoder when it is missing.

The review pages are standalone HTML. They need no install. The geocode review page loads Leaflet from a CDN.

Set `ANTHROPIC_API_KEY` before an extraction run. Scraping and geocoding call outside services and will spend API quota. Read the failures report and the geocode log before appending a volume to `databases/`.
