"""
Westfall Catalog dashboard builder -- Path B architecture.

DATA LAYER (pandas): reads/cleans the CSV, lays out coordinates, extracts
categories, and emits pure data as `data.json`.

PRESENTATION LAYER (MapLibre GL): a hand-authored vector map app plus the
carried-over Data tab, Info tab, and dashboard shell. MapLibre is the single
renderer -- no folium, no Leaflet, no bridge.

Run:  python build_site.py   ->   writes data.json and index.html
Serve the folder over http (e.g. `python -m http.server`) so data.json can be fetched.
"""

import pandas as pd
import numpy as np
import json
import math
import html as html_lib
import base64
import os

# ============================ CONFIG ============================
CSV_PATH = "cdsb_database.csv"
OUTPUT_HTML = "cdsb_visualization.html"
OUTPUT_DATA = "cdsb_data.json"

# ======================================================================
# BASEMAP
#
# 'ohm'      -- OpenHistoricalMap. A time-aware basemap: borders, place names and
#               features carry start/end dates, and the map is filtered to whatever
#               year the Temporal Controller is showing. Period names and changing
#               borders come for free, with no gazetteer to curate.
# 'maptiler' -- the previous modern vector basemap. Kept switchable: OHM's coverage
#               is volunteer-contributed and uneven, so this is the fallback.
#
# Two things differ per basemap and MUST travel together:
#   * the style URL
#   * the label font -- each style ships its own glyph server, and asking for a font
#     it does not serve renders labels BLANK rather than falling back. OHM's glyph
#     endpoint serves only the OpenHistorical family.
# ======================================================================
BASEMAP = 'ohm'          # 'ohm' | 'maptiler'

OHM_STYLE_URL = "https://www.openhistoricalmap.org/map-styles/main/main.json"

MAPTILER_STYLE_URL = ("https://api.maptiler.com/maps/"
                      "019ecd3a-a855-7629-bfc9-3ed552da7630/style.json"
                      "?key=WS5k0XeBJBUUDowtC8l1")

# OHM data is ODbL and its stylesheet ships with no attribution on any source, so the
# credit has to be added by hand. https://www.openhistoricalmap.org/copyright
OHM_ATTRIBUTION = ('<a href="https://www.openhistoricalmap.org/copyright" target="_blank" '
                   'rel="noopener">&copy; OpenHistoricalMap contributors</a>')

# Re-filtering the basemap rewrites the filter on every layer that has a source-layer
# (~246 of them in OHM's style). Playback ticks as fast as every 40 ms at 5x, so
# re-filtering on every tick would mean thousands of filter rewrites per second.
# Instead, only re-filter once the year has moved this far. 0 disables throttling.
BASEMAP_DATE_STEP = 5    # years

# Language for OHM's own labels (countries, towns, water...). OHM's stylesheet asks for
# ['get','name'], the LOCAL name -- so Paris renders as "Paris" but Wien as "Wien" and
# Muenchen as "Muenchen". OHM's tiles also carry localized names, and the field format
# is name_<lang> with an UNDERSCORE (not the name:en colon form used by raw OSM). That
# format is confirmed by OHM's own embed, which passes localizedNamePropertyFormat:
# "name_$1" to the Diplomat plugin.
#
# We do the substitution directly rather than using Diplomat, because every published
# Diplomat version requires maplibre-gl ^5, and its localized text-field uses the
# ["split"] expression, which MapLibre 4.7.1 rejects outright. See README for the
# upgrade path if the fuller Diplomat feature set is ever wanted.
#
# '' disables localization and leaves OHM's local-language labels alone.
OHM_LABEL_LANGUAGE = 'en'

# Parts of OHM's tile schema to STRIP from the basemap entirely, matched as prefixes
# against each layer's source-layer. These are dropped at load with removeLayer, not
# merely hidden: the catalog is about where scholars were born, studied and died, and
# modern road and rail geometry is noise against that.
#
#   transport_*  -> roads, railways, subways, tunnels, ferries, runways, taxiways,
#                   aerodromes, heliports, terminals, platforms, traffic signals
#   buildings*   -> building footprints and their points of interest
#
# Removing beats hiding on two counts: there is no control for a user to switch it back
# on, and the date filter no longer walks them -- 145 of OHM's 248 filterable layers are
# infrastructure, so every basemap date change does 58% less work, which matters most
# during 5x playback.
#
# Set to () to keep the full basemap.
OHM_INFRASTRUCTURE_PREFIXES = ('transport_', 'buildings')

# The view the map opens at.
#
# Fitted to these bounds rather than set as a fixed centre/zoom, because there is no
# single zoom that shows the world: it is about 1.95 on a desktop, 0.7 on a tablet and
# below 0 on a phone. A fixed number would open on the world for one of them and on a
# region for the others.
#
# Latitude stops short of the poles deliberately. Mercator stretches them into most of
# the frame, and the catalog reaches only 35S to 64N -- including Antarctica would push
# every scholar into a strip across the middle.
DEFAULT_VIEW_BOUNDS = ((-175.0, -58.0), (178.0, 76.0))   # (west, south), (east, north)

# Only used before the fit lands, and as a fallback if it cannot. A world-ish view, so a
# failure degrades to roughly the right thing rather than back to Europe.
FALLBACK_CENTER = (2.0, 25.0)
FALLBACK_ZOOM = 1.0

# The basemap's administrative boundaries, selected by source-layer. OHM keeps the whole
# admin hierarchy on one source-layer -- country, state, county and city lines plus their
# casings, eight layers in all -- so this is one entry rather than a list of ids.
#
# These are the LINES only. Country and state NAMES sit on land_ohm_centroids and belong
# to Place Labels. That split is the useful one: you can name France without drawing its
# outline, or draw the outline without naming it.
OHM_BORDER_SOURCE_LAYERS = ('land_ohm_lines',)

# The maplibre-gl-dates plugin (CC0), vendored rather than loaded from a CDN: the
# unscoped npm package was unpublished, and inlining keeps the deliverable self-contained.
OHM_DATES_PLUGIN = "vendor/maplibre-gl-dates.js"

def _filter_code_map():
    """Short codes for filter categories, used only to keep permalinks short.

    A category's initials ('scientific_disciplines' -> 'sd') rather than its index in
    FILTER_CATEGORIES: an index would silently point at the WRONG category the moment
    the list is reordered, exactly the trap the person-by-name choice avoids. Initials
    are stable under reordering and still legible in a URL.

    Collisions are a build error, not a runtime surprise.
    """
    out, seen = {}, {}
    for key, _col, _lbl in FILTER_CATEGORIES:
        code = ''.join(w[0] for w in key.split('_') if w)
        if code in seen:
            raise AssertionError(
                f"permalink category codes collide: {key!r} and {seen[code]!r} both "
                f"abbreviate to {code!r}. Rename one, or extend _filter_code_map().")
        seen[code] = key
        out[key] = code
    return out


def _basemap_style_url():
    return OHM_STYLE_URL if BASEMAP == 'ohm' else MAPTILER_STYLE_URL


# OHM's glyph server (www.openhistoricalmap.org/map-styles/fonts/{fontstack}/{range}.pbf)
# serves STATIC files, one directory per font. These are the only names it has -- OHM's
# own stylesheet uses exactly these, always alone, across all 56 of its symbol layers.
OHM_FONTS = ('OpenHistorical', 'OpenHistorical Bold', 'OpenHistorical Italic')


def _basemap_label_font():
    """The font stack for OUR label layers (the cluster counts).

    A `text-font` array is NOT a client-side fallback chain. MapLibre joins it with
    commas into ONE fontstack name and asks the glyph server for it:

        ['A', 'B']  ->  GET .../fonts/A,B/0-255.pbf

    Commercial servers (MapTiler, Mapbox) composite such a stack on the fly, which is
    why the multi-font Noto stack works there. OHM serves static directories, so a
    composite name 404s -- and a glyph 404 is not a cosmetic failure. MapLibre awaits
    glyphs with Promise.all before finalising ANY of a tile's buckets, so one missing
    fontstack aborts the whole tile: the cluster CIRCLES vanish along with their
    labels, while tiles holding no clustered features still render.

    So for OHM: exactly one font, and one it actually serves.
    """
    if BASEMAP == 'ohm':
        return ['OpenHistorical Bold']
    return ['Noto Sans Bold', 'Open Sans Bold', 'Arial Unicode MS Bold']


def _assert_label_font_is_servable():
    """Fail the build rather than ship a map whose clusters silently disappear."""
    font = _basemap_label_font()
    if BASEMAP != 'ohm':
        return
    if len(font) != 1:
        raise AssertionError(
            f"BASEMAP='ohm' needs a SINGLE font, got {font!r}. MapLibre would request the "
            f"fontstack {','.join(font)!r} as one name; OHM's static glyph server has no "
            f"such directory, the 404 aborts every tile that contains a label, and the "
            f"cluster circles disappear with it.")
    if font[0] not in OHM_FONTS:
        raise AssertionError(
            f"{font[0]!r} is not served by OHM's glyph endpoint. Choose one of {OHM_FONTS}.")


MAP_TITLE = "AI Global History of Science"
MAP_SUBTITLE = "Westfall Catalog"
FEEDBACK_URL = "https://forms.gle/ifRbvLj2BFH53wDx7"
OG_IMAGE_URL = ""
PAGE_DESCRIPTION = ("Interactive map and data explorer of early-modern scientists from the "
                    "Westfall catalog \u2014 birthplaces, deathplaces, life paths, disciplines, "
                    "patronage, religion, and more.")

# Beta toggles: park a tab behind a "coming soon" placeholder.
DATA_TAB_COMING_SOON = False
INFO_TAB_COMING_SOON = False

INFO_ABOUT         = ''
INFO_HOW_TO_USE    = ''
INFO_HOW_TO_CITE   = ''
INFO_WORKS_CITED   = ''
INFO_COPYRIGHT     = ''
INFO_AI_DISCLOSURE = ''

# ======================================================================
# INDEX TAB -- a dictionary of the dashboard's tools and terminology.
#
# Empty by design: add your own groups and terms below.
#
# Shape:  (group_id, 'Group Heading', 'optional blurb', [ (term, definition), ... ])
#   - group_id must be unique; it becomes the anchor used by the table of contents.
#   - `definition` accepts HTML: <p>, <ul><li>, <a href="...">, <code>, <em>.
#   - A term with an empty definition ('') still appears, marked "to be added".
#
# Example:
#
# INDEX_GROUPS = [
#     ('idx-map-tools', 'Map Tools', 'Controls found on the Map tab.', [
#         ('Advanced Query Builder', '<p>Builds a filter from category/value pills.</p>'),
#         ('Temporal Controller', ''),
#     ]),
# ]
# ======================================================================
INDEX_TAB_COMING_SOON = False

INDEX_INTRO = (
    '<p>Every control, marker and convention in this dashboard, grouped by where you '
    'meet it. If you are looking for what the catalog <em>is</em> and how it was '
    'compiled, that is the Info tab.</p>'
)

INDEX_GROUPS = [

    # ---------------------------------------------------------------- orientation
    ('idx-start', 'Getting Started', 'The four tabs, and what each is for.', [
        ('Map tab',
         '<p>The main view: every figure in the catalog placed where they were born, '
         'studied and died. Panels on the left <em>narrow</em> what is shown (by date, '
         'by attribute); panels on the right control <em>how</em> it is drawn and let you '
         'find one person.</p>'
         '<p>Nothing is shown until it is selected: with no filters applied, every mapped '
         'figure is included.</p>'),

        ('Data tab',
         '<p>The same catalog as a sortable, filterable table, plus a chart builder. Use it '
         'when you want to read the underlying records rather than look at geography.</p>'
         '<p>The Map and Data tabs <b>share their category filters</b>: add a filter in '
         'either and it appears in the other. Their date filters are separate, because the '
         'Map filters on a timeline and the table filters on a year column.</p>'),

        ('Index tab',
         '<p>This page: every control, marker and convention in the dashboard, grouped by '
         'where you meet it. Use the filter box to jump straight to a term \u2014 it '
         'searches the descriptions, not just the headings.</p>'
         '<p>It covers <em>how the dashboard works</em>. For what the catalog is and how it '
         'was compiled, see the Info tab.</p>'),

        ('Info tab',
         '<p>The project itself: what the catalog is, where it came from, how to cite it, '
         'and how it was compiled.</p>'),

        ('Copy link',
         '<p>Copies a URL that reopens the <b>exact view</b> you are looking at: the year '
         'window, the active filters, the map position, the selected person, which layers '
         'are on, and which tab.</p>'
         '<p>The address bar already tracks this as you work, so you can also just copy it. '
         'The button exists because that URL is easy to miss, and invisible on mobile.</p>'
         '<p>A link identifies a person <em>by name</em>, so it keeps pointing at the right '
         'person after the catalog is updated. If a figure or a filter value is later '
         'removed, that part of the link is quietly ignored rather than guessed at.</p>'),

        ('Feedback', '<p>Opens a form for reporting an error or suggesting a change.</p>'),

        ('BETA',
         '<p>This dashboard is under active development. Records are still being checked, '
         'and both the data and the interface may change.</p>'),
    ]),

    # ---------------------------------------------------------------- layers
    ('idx-layers', 'Layers', 'Right-hand panel. Controls what is drawn on the map.', [
        ('Birth Clusters',
         '<p>Green markers at each place of birth. <em>On by default.</em></p>'),

        ('Education Institutions',
         '<p>Orange markers at each place of study. One marker per <b>institution</b>, not '
         'per student: a university attended by fifty people is a single dot labelled 50.</p>'),

        ('Death Clusters',
         '<p>Red markers at each place of death.</p>'),

        ('Life Vectors',
         '<p>A grey line tracing each figure\u2019s path: birthplace \u2192 each place of '
         'education in the order attended \u2192 place of death. Someone with no recorded '
         'death place still has a line if they studied somewhere; someone with neither has '
         'no line, since a single point is not a path.</p>'),

        ('Birth Heatmap / Education Heatmap / Death Heatmap',
         '<p>Density shading rather than individual markers \u2014 useful for seeing where '
         'activity concentrated without counting dots. Fades out as you zoom in past the '
         'point where individual markers are readable.</p>'
         '<p>Heatmaps are drawn from the <b>true recorded coordinates</b>, not the '
         'slightly-separated marker positions (see <em>Marker separation</em>), so they show '
         'real density.</p>'
         '<p>The education heatmap counts <b>people</b>, not institutions: a university with '
         'fifty students burns fifty times brighter than one with a single student, even '
         'though both are one dot on the Education Institutions layer.</p>'),

        ('Basemap \u2014 Place Labels',
         '<p>The underlying map\u2019s own text: country, region, city and water names. Turn '
         'off for an uncluttered map when the labels compete with the markers.</p>'
         '<p>Does not affect the marker counts, which are ours, not the basemap\u2019s.</p>'),

        ('Basemap \u2014 Borders',
         '<p>Administrative boundary lines: country, state, county and city. These are '
         '<b>period borders</b> \u2014 they change with the year shown (see <em>Historical '
         'basemap</em>).</p>'
         '<p>Independent of Place Labels: country <em>names</em> are labels, country '
         '<em>outlines</em> are borders. You can have either without the other.</p>'),

        ('Cluster',
         '<p>When markers are too close together to read, they merge into one circle showing '
         'how many are inside. <b>Click a cluster to zoom in and break it apart.</b> Clusters '
         'dissolve into individual markers by zoom level 10.</p>'
         '<p>An education cluster counts <b>people</b>, not institutions, so it means the '
         'same thing as a birth or death cluster.</p>'),

        ('Marker separation',
         '<p>Markers at identical coordinates would sit exactly on top of each other, making '
         'all but the top one unclickable. Three things prevent that: busy places (more than '
         'five figures) are fanned onto a small ring; everything else is nudged by about '
         '100 m; and where one person was born and died in the same place, the two markers '
         'are pushed a few hundred metres apart in opposite directions.</p>'
         '<p>So a marker\u2019s position is accurate to the <em>place</em>, not the street. '
         'The heatmaps and the Data tab use the true coordinates.</p>'),
    ]),

    # ---------------------------------------------------------------- temporal
    ('idx-temporal', 'Temporal Controller',
     'Left-hand panel, top. Restricts the map to a period, and animates through it.', [
        ('Filter by Timeline',
         '<p>Turns the date filter on. While off, every figure is shown regardless of date '
         'and the readout says <em>All years</em>.</p>'),

        ('Min / Max',
         '<p>The two ends of the year window. Drag either handle; they cannot cross.</p>'),

        ('Alive in range',
         '<p>What the timeline actually selects. A figure is included when their '
         '<b>lifetime overlaps the window</b> \u2014 not when they were born inside it. '
         'Someone born 1590 and died 1660 appears in a 1600\u20131650 window, because they '
         'were alive throughout it.</p>'
         '<p>Where only one year is recorded, that year must fall inside the window. Where '
         'neither is recorded, the figure is excluded whenever the timeline is on \u2014 an '
         'undated person cannot be shown to be alive at any particular time.</p>'),

        ('Playback',
         '<p>Reveals the Play controls, which sweep a single year forward through the '
         'timeline so you can watch activity move across the map.</p>'),

        ('Play / Pause',
         '<p>Starts and stops the sweep. Play from a paused position resumes; play from the '
         'end starts over. Pausing leaves the map exactly on the year shown.</p>'),

        ('Speed',
         '<p>0.25x to 2.00x, plus 5.00x. One year per step; the speed sets how fast the '
         'steps run.</p>'),

        ('Current Year',
         '<p>The year, or year range, the map is currently showing. During playback this is '
         'a single year; when you set a window by hand it shows both ends.</p>'),
    ]),

    # ---------------------------------------------------------------- query builder
    ('idx-query', 'Advanced Query Builder',
     'Left-hand panel. Filters the map by any recorded attribute.', [
        ('Category dropdowns',
         '<p>One per filterable field \u2014 discipline, instrument, society, institution, '
         'degree, means of support, patronage, gender, religion, family wealth, father\u2019s '
         'occupation, country of birth, country of death. Choosing a value adds it to '
         '<em>Active Filters</em>.</p>'),

        ('Active Filters',
         '<p>The filters currently applied, as removable chips. Click the \u00d7 on a chip to '
         'drop that filter.</p>'
         '<p>These chips are <b>shared with the Data tab</b>: add one here and it appears '
         'there too, and vice versa.</p>'),

        ('Match: ALL (AND)',
         '<p>A figure must satisfy <b>every</b> active filter. Narrows as you add filters. '
         'This is the default.</p>'
         '<p><em>Astronomy AND Jesuit</em> \u2192 only Jesuit astronomers.</p>'),

        ('Match: ANY (OR)',
         '<p>A figure needs satisfy only <b>one</b> active filter. Widens as you add filters.'
         '</p><p><em>Astronomy OR Jesuit</em> \u2192 every astronomer, plus every Jesuit.</p>'),

        ('Apply Query',
         '<p>Redraws the map against the current filters. Adding or removing a chip already '
         'redraws, so this is mostly a way to force a refresh.</p>'),

        ('Clear',
         '<p>Removes every active filter. Does not touch the timeline or the layers.</p>'),

        ('Export selection (CSV)',
         '<p>Downloads the currently selected figures as a spreadsheet \u2014 whatever '
         'survives the timeline and the filters \u2014 with their coordinates, dates and '
         'every catalogued attribute.</p>'
         '<p>The filename records the year window and the head count, so exports stay '
         'distinguishable from one another.</p>'),
    ]),

    # ---------------------------------------------------------------- people
    ('idx-people', 'Finding and Reading a Person',
     'Right-hand panel, and what happens when you click.', [
        ('Find a Person',
         '<p>Type any part of a name. Choosing a suggestion selects that figure: the map '
         'frames their whole life path and opens their record.</p>'
         '<p>Searches <b>alternate names as well as the catalogued one</b>, so a figure is '
         'findable by the name you know rather than the one the compiler filed them under. '
         'Typing <em>Abelard</em> finds Pierre Abailard; <em>Albatenius</em> finds '
         'al-Batt\u0101n\u012b. Where a match came from an alternate name, the suggestion '
         'says which one, so an unexpected result explains itself.</p>'
         '<p>Suggestions show a birth year to separate figures who share a name. A match on '
         'the catalogued name is listed before a match on an alternate one.</p>'
         '<p>Only figures with a mapped birthplace appear here. Anyone whose coordinates '
         'were never recorded has no marker to select \u2014 use the Data tab, which lists '
         'every catalogued figure whether or not they can be placed.</p>'),

        ('Selection highlight',
         '<p>The selected figure is drawn in purple, with a dashed line along their path, on '
         'top of everything else. It stays visible while you pan, filter, or run playback.</p>'),

        ('Clear selection',
         '<p>Drops the highlight and closes the record. Appears only while someone is '
         'selected.</p>'),

        ('Person record',
         '<p>The panel that opens when you click a marker. Lists what the catalog holds: '
         'alternate names, birth and death (place and year), gender, education, father, '
         'disciplines, societies, religion, means of support, patronage, instruments, and '
         'the sources the entry was compiled from.</p>'
         '<p>Fields with nothing recorded are left out rather than shown empty, so the record '
         'shows what <em>is</em> known.</p>'),

        ('Life vector tooltip',
         '<p>Hovering a life vector names the figure and the chain of places their line '
         'passes through, in order. Consecutive repeats collapse, so someone born and '
         'educated in the same city reads <em>Paris \u2192 Padua</em>, not <em>Paris \u2192 '
         'Paris \u2192 Padua</em>.</p>'),

        ('Synchronised hover',
         '<p>Hovering any one of a figure\u2019s markers highlights all of them at once \u2014 '
         'birth, education stops, death, and the line joining them \u2014 so you can see a '
         'whole life without clicking. Only layers that are switched on light up.</p>'),

        ('Legend',
         '<p>Bottom right. Hover any entry for a fuller description of what the colour '
         'means.</p>'),
    ]),

    # ---------------------------------------------------------------- education
    ('idx-education', 'Education', 'What the orange markers hold.', [
        ('Education bubble',
         '<p>One marker per institution, sized and labelled by how many of the '
         '<b>currently selected</b> figures studied there. Change the filters and the '
         'numbers change with them.</p>'
         '<p>A dot with a single student is drawn small and unlabelled \u2014 a "1" on every '
         'lone marker would be noise.</p>'),

        ('Grouped by institution',
         '<p>Bubbles group by <b>institution name</b>, not by coordinate. One geocode often '
         'covers many separate schools \u2014 twenty-one distinct institutions share the '
         'centre of Paris \u2014 and grouping by position would merge them into a single '
         'meaningless dot. Institutions sharing a point are fanned onto a small ring so each '
         'stays separately clickable.</p>'),

        ('Not Specified',
         '<p>A bubble pooling everyone recorded as having studied in a <b>city</b> whose '
         'institution the source never names. It sits alongside that city\u2019s named '
         'institutions and carries a note saying so.</p>'
         '<p>It is <b>not</b> a school, and not a spelling variant of one. Paris, for example, '
         'has twenty-one named institutions plus one <em>Not Specified</em> bubble.</p>'),

        ('Roster',
         '<p>Everyone who studied at that institution under the current filters, with the '
         'years they were there, earliest first. Undated figures fall to the bottom.</p>'),

        ('View timeline',
         '<p>Opens a chart of who was at an institution when \u2014 one bar per stay, drawn '
         'against a shared year axis, so overlapping figures line up vertically. This is how '
         'you spot who might plausibly have met.</p>'
         '<p>Click any row to find that person on the map. Close with \u00d7, by clicking '
         'outside, or with <kbd>Esc</kbd>. The button appears only when at least one figure '
         'there has a recorded year.</p>'),

        ('Open-ended bar',
         '<p>On the timeline, a bar that fades out at one end means the source records only '
         '<b>one</b> of the two years. The fade marks where knowledge stops \u2014 it is not '
         'an estimate of when they left.</p>'
         '<p>Figures with no years at all are not plotted; they are listed under the axis, '
         'so they are visible without inventing a position for them.</p>'),

        ('Recorded stays',
         '<p>The timeline subtitle may report more stays than people. That is not an error: '
         'someone can have two spells at one institution. Aquinas was at Paris twice.</p>'),
    ]),

    # ---------------------------------------------------------------- basemap
    ('idx-basemap', 'The Map Itself', 'The historical basemap under the data.', [
        ('Historical basemap',
         '<p>The underlying map is <a href="https://www.openhistoricalmap.org/" '
         'target="_blank" rel="noopener">OpenHistoricalMap</a>, in which places and '
         'boundaries carry <b>start and end dates</b>. It is filtered to whatever year the '
         'Temporal Controller shows, so borders, place names and settlements are those of '
         'the period \u2014 not modern ones.</p>'
         '<p>With a range selected, the basemap shows the world as of the <b>end</b> of the '
         'window. With the timeline off, it shows the latest year in the catalog.</p>'
         '<p>Coverage is contributed by volunteers and is uneven: sparse coverage means '
         'missing borders in some periods, not a mistake in the catalog.</p>'),

        ('Place names in English',
         '<p>Basemap labels use the English name where OpenHistoricalMap records one, and '
         'fall back to the local name where it does not \u2014 which, for many historical '
         'places, is the only name there is.</p>'),

        ('Infrastructure',
         '<p>Roads, railways, airports and buildings are removed from the basemap. They are '
         'modern, and irrelevant to where a 16th-century scholar studied.</p>'),

        ('Attribution',
         '<p>Bottom right. Credits OpenHistoricalMap\u2019s contributors, whose data is '
         'licensed ODbL. Required by that licence \u2014 please leave it in place.</p>'),

        ('Scale bar',
         '<p>Under the legend. Shows the distance a screen length represents at the current '
         'zoom.</p>'),

        ('Opening view',
         '<p>The map opens on the whole world. The catalog reaches from San Francisco to '
         'Japan, and opening on Europe would hide an eighth of it behind a pan you had no '
         'reason to make \u2014 even though most of the figures are European.</p>'
         '<p>The exact zoom is fitted to your window rather than fixed, since the zoom that '
         'shows the world on a desktop shows only a region on a phone.</p>'),

        ('Zoom',
         '<p>Bottom right, or scroll / pinch. The map draws a single world rather than '
         'repeating east\u2013west, so a life vector never wraps around the back of the '
         'globe.</p>'),
    ]),

    # ---------------------------------------------------------------- data tab
    ('idx-data', 'Data Explorer', 'The Data tab: the catalog as a table.', [
        ('Search all columns',
         '<p>Free text across every column at once \u2014 including <em>Also Known As</em>, '
         'so alternate names are searchable here too. Matches anywhere, not just at the '
         'start of a word.</p>'),

        ('Also Known As',
         '<p>A column listing a figure\u2019s alternate names. Unlike the Map\u2019s search '
         'box, the table covers <b>every catalogued figure</b>, including those with no '
         'coordinates who never appear on the map.</p>'),

        ('Year range',
         '<p>Pick <em>Born year</em> or <em>Died year</em>, then a minimum and/or maximum. '
         'Either end can be left blank for an open bound.</p>'
         '<p>This is a plain column filter and behaves differently from the Map\u2019s '
         'timeline, which asks whether someone was <em>alive</em> during a window.</p>'),

        ('Match ALL / ANY',
         '<p>As in the Query Builder: ALL requires every chip, ANY requires only one. Shared '
         'with the Map.</p>'),

        ('Add filter: category',
         '<p>Pick a category, then a value, to add a filter chip. The same chips as the '
         'Map\u2019s Active Filters.</p>'),

        ('Clear',
         '<p>Drops every filter chip, the search text, and the year range. Table only.</p>'),

        ('Export filtered (CSV)',
         '<p>Downloads exactly the rows currently shown, with every column. The filename '
         'records the row count.</p>'
         '<p>Years export as raw numbers, so BC years appear as negatives (\u2013384, not '
         '"384 BC"). That keeps the file sortable and computable in a spreadsheet.</p>'),

        ('Hide charts',
         '<p>Collapses the chart panel to give the table the full width.</p>'),

        ('Column sorting',
         '<p>Click any column heading to sort; click again to reverse. Blanks always sort '
         'last, in either direction, so an empty cell never displaces a real value from the '
         'top.</p>'),

        ('Reordering columns',
         '<p>Drag a column heading sideways to move it; it drops in <em>before</em> the '
         'heading you release on. A grip appears on the left of each heading on hover.</p>'
         '<p>The new order carries through everything the table drives \u2014 the rows, the '
         'record detail, and the CSV export all follow it \u2014 and survives filtering and '
         'sorting. Dragging and click-to-sort share the same heading and do not interfere: a '
         'drag reorders, a click sorts.</p>'
         '<p>The order is not saved between visits; a reload restores the default.</p>'),

        ('Summary strip',
         '<p>Above the table: how many figures are in view, the span of their birth years, '
         'and the three commonest disciplines among them. Updates with every filter.</p>'),

        ('Row detail',
         '<p>Click any row for the full record, including long note fields the table '
         'truncates.</p>'),

        ('Show on map',
         '<p>In the row detail: jumps to the Map tab and selects that figure. Disabled when '
         'a record has no birth coordinates, since there is no marker to show.</p>'),

        ('Data-quality badge',
         '<p>At the top of each row detail: how many of the compiled sources '
         '<b>disagreed</b> about this figure, and on which fields. Green for none, amber for '
         'one or two, red beyond that.</p>'
         '<p>It measures source agreement, not correctness: sources can agree and still be '
         'wrong. Treat a red badge as a prompt to check the primary sources yourself.</p>'),
    ]),

    # ---------------------------------------------------------------- charts
    ('idx-charts', 'Chart Builder',
     'Data tab, right-hand panel. Charts whatever the table currently shows.', [
        ('What gets charted',
         '<p>Always the <b>filtered</b> rows, never the whole catalog. The chart\u2019s '
         'subtitle records which filters were active and how many figures they matched, so a '
         'chart exported to PNG still says what it is a chart of.</p>'),

        ('Type',
         '<p><b>Bar</b> \u2014 compare categories. <b>Line</b> / <b>Area</b> \u2014 change '
         'over time. <b>Pie</b> / <b>Doughnut</b> \u2014 shares of a whole. '
         '<b>Histogram</b> \u2014 the distribution of one number. <b>Scatter</b> \u2014 two '
         'numbers against each other.</p>'
         '<p>The controls below change with the type: only the ones that apply are shown.</p>'),

        ('Group by',
         '<p>What forms the categories, or the x-axis. Pick a text field (discipline, '
         'country) or a number (born year).</p>'),

        ('Bin size',
         '<p>When grouping by a number, how wide each bucket is. 10 gives decades, 100 gives '
         'centuries.</p>'),

        ('Split by',
         '<p>Breaks each bar or line into sub-series by a second field \u2014 disciplines by '
         'decade, split by religion. Limited to the eight commonest values.</p>'),

        ('Measure / of field',
         '<p>What the height means: <b>Count</b> of figures (the default), or the '
         '<b>Average</b>, <b>Median</b>, <b>Sum</b>, <b>Min</b> or <b>Max</b> of a chosen '
         'numeric field \u2014 average lifespan by country, for instance.</p>'),

        ('Y axis / Color by',
         '<p>Scatter only: the second number to plot, and an optional field to colour the '
         'points by.</p>'),

        ('Sort',
         '<p><em>Value \u2193</em> or <em>Value \u2191</em> orders the categories by size; '
         '<em>Label A\u2013Z</em> orders them alphabetically.</p>'
         '<p>Numeric groupings always run in numeric order regardless, since a chronology '
         'out of order is not a chronology.</p>'),

        ('Exclude Unknown',
         '<p>Drops <em>Unknown</em> from the chart\u2019s categories. The catalog records '
         '<em>Unknown</em> as an answer rather than leaving a blank, so it charts as a '
         'category like any other \u2014 and it is often the biggest one: religion is '
         'Unknown for 228 of 366 figures, father\u2019s wealth for 270.</p>'
         '<p>Also drops <em>Not Specified</em>, which is what the sources say when an '
         'institution was not recorded.</p>'
         '<p>Off by default: the catalog says <em>Unknown</em>, and hiding that unasked '
         'would overstate how much is known. When it is on, the chart\u2019s subtitle says '
         'so \u2014 an exported PNG travels without this panel, so it has to carry the '
         'caveat itself.</p>'
         '<p>What is excluded is the <em>value</em>, not the figure: someone recorded as '
         '"Astronomy; Unknown" still counts under Astronomy. Numeric axes are unaffected, '
         'since a missing number is blank rather than "Unknown".</p>'),

        ('Max groups / Remainder as “Other”',
         '<p>Caps how many categories are drawn, so a long tail does not crush the chart. '
         'Note this is unrelated to <em>Exclude Unknown</em>: \u201cOther\u201d is a bucket '
         'this control creates, not a value the sources recorded.</p>'
         '<p>Tick <em>Remainder as “Other”</em> to roll everything past the cap into one '
         'bar rather than drop it silently.</p>'),

        ('Bins',
         '<p>Histogram only: how many buckets to divide the range into.</p>'),

        ('Horizontal / Stacked',
         '<p>Bar charts: turn sideways when category names are long; stack a split series to '
         'read totals rather than compare parts.</p>'),

        ('Build chart / PNG',
         '<p>Draws the chart, and downloads it as an image. The PNG carries the title and '
         'filter summary with it.</p>'),
    ]),

    # ---------------------------------------------------------------- conventions
    ('idx-conventions', 'Conventions and Caveats',
     'How to read what the dashboard shows \u2014 and what it does not claim.', [
        ('BC years',
         '<p>Displayed as <em>384 BC</em> everywhere a reader sees them. AD years are shown '
         'plainly, with no era suffix, since the catalog is overwhelmingly post-classical '
         'and the suffix would be noise on almost every row.</p>'
         '<p>Underneath, a BC year is stored as a negative number, which is why timelines, '
         'sorting and lifespan arithmetic work across year zero. CSV exports keep the '
         'negative form.</p>'),

        ('Unknown',
         '<p>The catalog records <em>Unknown</em> as a value in its own right. Treated as '
         '"nothing recorded" and left out of the display rather than shown as the word '
         '"Unknown".</p>'),

        ('????',
         '<p>In an education roster or timeline: this year is not recorded. Shown rather '
         'than hidden, so a missing year is visibly missing instead of silently absent.</p>'),

        ('Alternate names',
         '<p>The Latin, Arabic and vernacular forms a figure is also known by \u2014 '
         '<em>Peter Abelard</em> for Pierre Abailard, <em>Savasorda</em> for Abraham Bar '
         '\u1e24iyya, <em>Albertus Magnus</em> for Albert the Great. About half the catalog '
         'carries at least one.</p>'
         '<p>They are searchable on both tabs, and listed in a figure\u2019s record under '
         '<em>Also known as</em>. The Data tab shows them in its own column.</p>'
         '<p>Which form is treated as the main name is the compiler\u2019s choice, not a '
         'judgement about which is correct.</p>'),

        ('Names',
         '<p>Shown as a reader would say them. Where the source splits a name awkwardly, the '
         'display corrects it: <em>Albert of Saxony</em>, not "of Saxony Albert"; '
         '<em>Aristotle</em>, not "Aristotle Aristotle".</p>'
         '<p>The Data tab files names by surname for sorting \u2014 <em>Galilei, '
         'Galileo</em> \u2014 but an epithet takes no comma, since "of Saxony" is not a '
         'first name.</p>'),

        ('Lifespan',
         '<p>Died year minus born year. A count of years, not a date, so it is never '
         'labelled BC.</p>'),

        ('One record, several people',
         '<p>A few entries cover more than one person \u2014 the Ban\u016b M\u016bs\u0101 '
         'record is three brothers. They map as a single point.</p>'),

        ('Absence is not evidence',
         '<p>An empty region may mean nobody worked there, or that Westfall\u2019s catalog '
         'does not cover it. The catalog is a compilation with its own scope and biases, and '
         'the map inherits every one of them. See the Info tab.</p>'),

        ('Keyboard',
         '<p><kbd>Esc</kbd> closes the education timeline, the record detail, and the search '
         'suggestions. <kbd>Enter</kbd> in the person search picks the first match.</p>'),

        ('Filter this index',
         '<p>The box at the top of this page hides any entry that does not match what you '
         'type. It searches the descriptions, not just the headings.</p>'),
    ]),
]

# ======================================================================
# UNTESTED FEATURES
#
# Scientific Discipline, Means of Support and Patronage Types have not been checked yet,
# so they are hidden by default. Flip this to True to bring them back everywhere at once.
#
# The three appear across several config lists (filters, query dropdowns, Data-tab
# columns, note columns) plus two hardcoded spots (the map popup, one stats line). Rather
# than edit each site, we filter the lists here by key -- everything downstream is built
# from those lists, so hiding the keys hides the feature end to end: no filter chip, no
# dropdown, no column, no popup row, and no permalink code (so an old link that named one
# of these filters simply ignores it rather than breaking).
# ======================================================================
SHOW_UNTESTED_FIELDS = False

# The field keys governed by the flag, and the note-column keys that pair with them.
_UNTESTED_KEYS = frozenset({
    'scientific_disciplines', 'means_of_support', 'patronage_types',
    'scientific_disciplines_notes', 'means_of_support_notes', 'patronage_notes',
})


def _hidden_popup_labels():
    """Popup-row labels to drop when the untested fields are hidden.

    The popup builds from a label-keyed array in JS, not from the config lists, so it
    is filtered by label here rather than by internal key.
    """
    if SHOW_UNTESTED_FIELDS:
        return []
    return ['Scientific Disciplines', 'Means of Support', 'Patronage Types']


def _visible_specs(specs):
    """Drop the untested fields from a (key, ...) spec list unless the flag is on.

    Matches on the first tuple element (the internal key), which is stable, rather than
    on the human label, which is not.
    """
    if SHOW_UNTESTED_FIELDS:
        return specs
    return [row for row in specs if row[0] not in _UNTESTED_KEYS]


FILTER_CATEGORIES = _visible_specs([
    ('scientific_disciplines', 'Scientific_Disciplines', 'Scientific Disciplines'),
    ('instruments', 'Instruments', 'Instruments'),
    ('scientific_societies_membership', 'Scientific_Societies_Membership', 'Societies'),
    ('education_institution', 'Education_Institution', 'Educational Institution'),
    ('education_degrees', 'Education_Degrees', 'Degrees'),
    ('means_of_support', 'Means_of_Support', 'Means of Support'),
    ('patronage_types', 'Patronage_Types', 'Patronage Types'),
    ('gender', 'Gender', 'Gender'),
    ('religion_affiliation', 'Religion_Affiliation', 'Religion'),
    ('father_is_wealthy', 'Father_Is_Wealthy', 'Family Wealth'),
    ('father_occupation', 'Father_Occupation', 'Father Occupation'),
    ('born_country', 'Born_Country', 'Born Country'),
    ('died_country', 'Died_Country', 'Died Country'),
])

# Older exports used different names for the same fields. The column mapper falls back
# to these so a pre-rename CSV still builds.
COLUMN_ALIASES = {
    'instruments': ['Technological_Involvement_Type'],
    'instruments_notes': ['Technological_Involvement_Notes'],
}

# Free-text columns carried for reference/export only (never filterable).
# Alternate names: the Latin, Arabic and vernacular forms a figure is also known by.
# Comma-separated, and unlike Education_Institution the commas really are separators --
# no single alias in the data contains one. Half the catalog has at least one, and for
# pre-modern figures the alias is often the name a reader actually knows.
ALT_NAME_COL = 'Alt_Name'

EDUCATION_COORDS_COL = 'Education_Coordinates'   # "lat,lon;lat,lon" -- one per institution
EDUCATION_LOCS_COL = 'Education_Locations'       # "Town, Country, Start, End; ..." -- same order
QUALITY_COLUMNS = [
    ('disagreement_count', 'Disagreement_Count', 'Disagreement Count'),
    ('inconsistent_fields', 'Inconsistent_Fields', 'Inconsistent Fields'),
]

NOTE_CATEGORIES = _visible_specs([
    ('father_notes', 'Father_Notes', 'Father Notes'),
    ('education_notes', 'Education_Notes', 'Education Notes'),
    ('religion_notes', 'Religion_Notes', 'Religion Notes'),
    ('scientific_disciplines_notes', 'Scientific_Disciplines_Notes', 'Scientific Disciplines Notes'),
    ('means_of_support_notes', 'Means_of_Support_Notes', 'Means of Support Notes'),
    ('patronage_notes', 'Patronage_Notes', 'Patronage Notes'),
    ('instruments_notes', 'Instruments_Notes', 'Instruments Notes'),
    ('scientific_societies_notes', 'Scientific_Societies_Notes', 'Scientific Societies Notes'),
    ('compiler_details_notes', 'Compiler_Details_Notes', 'Compiler Details Notes'),
])

DATA_TABLE_COLUMNS = _visible_specs([
    ('name', 'Name', 'text'),
    ('alt_name', 'Also Known As', 'text'),
    ('gender', 'Gender', 'cat'),
    ('born_year', 'Born Year', 'number'),
    ('born_town', 'Born Town', 'text'),
    ('born_country', 'Born Country', 'cat'),
    ('died_year', 'Died Year', 'number'),
    ('died_town', 'Died Town', 'text'),
    ('died_country', 'Died Country', 'cat'),
    ('lifespan', 'Lifespan', 'number'),
    ('scientific_disciplines', 'Scientific Disciplines', 'multi'),
    ('instruments', 'Instruments', 'multi'),
    ('scientific_societies_membership', 'Societies', 'multi'),
    ('education_institution', 'Educational Institution', 'multi'),
    ('education_degrees', 'Degrees', 'multi'),
    ('means_of_support', 'Means of Support', 'multi'),
    ('patronage_types', 'Patronage Types', 'multi'),
    ('religion_affiliation', 'Religion', 'multi'),
    ('father_is_wealthy', 'Family Wealth', 'cat'),
    ('father_occupation', 'Father Occupation', 'cat'),
    ('father_notes', 'Father Notes', 'text'),
    ('education_notes', 'Education Notes', 'text'),
    ('religion_notes', 'Religion Notes', 'text'),
    ('scientific_disciplines_notes', 'Sci. Disciplines Notes', 'text'),
    ('means_of_support_notes', 'Means of Support Notes', 'text'),
    ('patronage_notes', 'Patronage Notes', 'text'),
    ('instruments_notes', 'Instruments Notes', 'text'),
    ('scientific_societies_notes', 'Societies Notes', 'text'),
    ('compiler_details_notes', 'Compiler Details Notes', 'text'),
    # NOTE: education_stops, disagreement_count and inconsistent_fields are still
    # carried on each record -- they power the detail modal's data-quality badge --
    # but they are deliberately NOT table columns.
])

WEALTH_DISPLAY = {'-1': 'Unknown', '0': 'Poor', '1': 'Wealthy'}

# Values that mean "the source did not record this", for the chart builder's
# "Exclude Unknown" toggle. Matched case-insensitively against a whole value, never a
# substring -- a discipline of "Unknown Mechanics" would be a real answer, and a
# substring rule would silently drop it.
#
# 'Not Specified' earns its place: it is in the source data (83 rows), it means the
# institution was not recorded, and on any institution chart it is the largest bar.
#
# Deliberately NOT here: 'Other'. That is a real category the compiler chose, not an
# absence of one. Blanks need no entry -- they already produce no value to group on.
CHART_UNKNOWN_TOKENS = ('unknown', 'not specified')

# Columns that hold a YEAR rather than a quantity. Named explicitly rather than inferred
# from the 'number' type, because `lifespan` is also a number -- but it is a DURATION,
# and rendering a 62-year life as "62 BC" would be nonsense.
YEAR_COLUMNS = ('born_year', 'died_year')

# One formatter, injected into both the map app and the Data tab, so a year can never
# read one way on the map and another in the table.
#
# Years are stored NEGATIVE for BC throughout the data layer, which is what lets them
# sort, subtract, bin and drive the timeline slider like any other number. Only the
# display changes: -350 reads as "350 BC". Positive years are shown bare, without "AD",
# since the catalog is overwhelmingly post-classical and the suffix would be noise on
# every single row.
#
# Year 0 is left as "0". Historians number 1 BC -> AD 1 with no year zero, so a 0 here
# means the source is using astronomical numbering (where 0 IS 1 BC) or is simply wrong;
# either way that is a data question, not something to paper over with a guess.
FMT_YEAR_JS = r"""
  function fmtYear(y){
    if (y === null || y === undefined || y === '') return '';
    var n = (typeof y === 'number') ? y : parseInt(String(y).trim(), 10);
    if (isNaN(n)) return String(y);
    return n < 0 ? (-n) + ' BC' : String(n);
  }
  // A year range, with the suffix carried by whichever end needs it: "350 BC - 20 BC",
  // "50 BC - AD 20" would be over-engineering, so it renders "50 BC - 20".
  function fmtYearRange(a, b, sep){
    return fmtYear(a) + (sep || '\u2013') + fmtYear(b);
  }
"""


def _norm(s):
    return s.lower().replace('_', '').replace(' ', '')


def _blank(v):
    """Treat NaN, empty, and the literal string 'Unknown' as no-data. The CSV uses
    'Unknown' as a value, not a null, so a plain isna() check is not enough."""
    if v is None:
        return True
    s = str(v).strip()
    return s == '' or s.lower() in ('nan', 'none', 'unknown')


def _year_or_unknown(v):
    """Education start/end years are frequently the literal 'Unknown'. Surface that
    word to the reader rather than a blank or a fabricated year."""
    if _blank(v):
        return 'Unknown'
    try:
        return str(int(float(str(v).strip())))
    except (ValueError, TypeError):
        return str(v).strip()


def _norm_int_str(s):
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
    except (ValueError, TypeError):
        pass
    return s


def _parse_edu_coords(raw):
    """Education_Coordinates: "lat,lon;lat,lon". Returns [(lat, lon), ...].
    Skips blanks and the literal string 'Unknown', which appears in real rows."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return []
    out = []
    for seg in str(raw).split(';'):
        seg = seg.strip()
        if not seg or seg.lower() == 'unknown':
            continue
        parts = [p.strip() for p in seg.split(',')]
        if len(parts) < 2:
            continue
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except (ValueError, TypeError):
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            out.append((lat, lon))
    return out


def _parse_edu_locations(raw):
    """Education_Locations: "Town, Country, Start, End; ...", same order as the
    coordinates. Town names may themselves contain commas, so the four fields are
    taken from the RIGHT: the last three are country/start/end."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return []
    out = []
    for seg in str(raw).split(';'):
        seg = seg.strip()
        if not seg or seg.lower() == 'unknown':
            continue
        p = [x.strip() for x in seg.split(',')]
        if len(p) >= 4:
            # A town holding a comma ("Middletown, Connecticut") arrives split across
            # several fields; rejoin it with the comma AND the space it was written with.
            out.append({'town': ', '.join(p[:-3]).strip(), 'country': p[-3],
                        'start': p[-2], 'end': p[-1]})
        else:
            out.append({'town': seg, 'country': '', 'start': '', 'end': ''})
    return out


# The source records a city-only study ("studied in Paris") with this sentinel rather
# than an institution name. It is NOT a school: every such stop in one city is pooled
# into a single "Not Specified" bubble alongside that city's named institutions.
NOT_SPECIFIED = 'not specified'


def _parse_edu_institutions(raw):
    """Education_Institution is a SEMICOLON-separated list, in the same order as
    Education_Coordinates. Semicolons matter: institution names legitimately contain
    commas ("St. John's College, Cambridge"), so splitting on commas would shred one
    name into several and knock the list out of alignment with the coordinates."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return []
    s = str(raw).strip()
    if not s or s.lower() == 'unknown':
        return []
    return [x.strip() for x in s.split(';') if x.strip()]


# The catalog's First_Name/Last_Name split does not fit pre-modern figures, and it
# breaks in two recorded ways:
#
#   1. TOPONYMIC EPITHET -- First_Name holds "of Saxony" and Last_Name holds "Albert".
#      Joined naively that reads "of Saxony Albert" instead of "Albert of Saxony".
#      19 rows. Every lowercase-leading First_Name in the data is one of these, and no
#      Last_Name leads with a particle, so keying on the prefix is unambiguous.
#
#   2. DOUBLED MONONYM -- a figure with one name has it in BOTH fields, giving
#      "Aristotle Aristotle". 6 rows.
#
# 'the ' is included for the same reason as 'of ' ("Pliny the Elder"): no given name
# begins with either word, so neither can produce a false positive. Add particles here
# ('ibn ', 'de ', 'van ') if the scale-up brings them.
NAME_EPITHET_PREFIXES = ('of ', 'the ')


def _looks_like_epithet(first):
    f = ' '.join((first or '').split()).lower()
    return any(f.startswith(p) for p in NAME_EPITHET_PREFIXES)


def _name_parts(first, last):
    return ' '.join((first or '').split()), ' '.join((last or '').split())


def format_name_display(first, last):
    """Map/popup name: how a reader would actually say it.

    'Galileo Galilei', but 'Albert of Saxony' rather than 'of Saxony Albert', and
    'Aristotle' rather than 'Aristotle Aristotle'.
    """
    first, last = _name_parts(first, last)
    if not first:
        return last
    if not last:
        return first
    if first.lower() == last.lower():
        return last                       # mononym recorded twice
    if _looks_like_epithet(first):
        return last + ' ' + first         # "Albert" + "of Saxony"
    return first + ' ' + last


def parse_alt_names(raw, display_name=''):
    """Alt_Name -> a list of distinct aliases.

    Split on commas: verified against the data, no alias contains one (contrast
    Education_Institution, where "St. John's College, Cambridge" is a single name and
    splitting on commas would shred it).

    Aliases identical to the displayed name are dropped -- they would add nothing to a
    search and would clutter the record with a name already at the top of it.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return []
    s = str(raw).strip()
    if not s or s.lower() in ('nan', 'none', 'unknown'):
        return []
    seen, out = set(), []
    dn = ' '.join(display_name.split()).lower()
    for part in s.split(','):
        n = ' '.join(part.split())
        k = n.lower()
        if not n or k in seen or k == dn:
            continue
        seen.add(k)
        out.append(n)
    return out


def format_name_last_first(first, last):
    """Data-tab name format, sorted by surname: 'Galilei, Galileo'.

    A missing half must not leave a dangling comma, so plain 'Galilei' / 'Galileo'
    when only one is recorded. An epithet takes NO comma -- "Albert, of Saxony" would
    imply "of Saxony" is a given name -- and it still files under A, which is where a
    reader looking for Albert of Saxony would go.
    """
    first, last = _name_parts(first, last)
    if not (first and last):
        return last or first
    if first.lower() == last.lower():
        return last
    if _looks_like_epithet(first):
        return last + ' ' + first
    return last + ', ' + first


def _clean_token(v):
    s = '' if v is None else str(v).strip()
    return '' if s.lower() in ('', 'nan', 'none', 'unknown') else s


def build_education_stops(coords_raw, locs_raw, insts_raw):
    """Zip the three parallel education columns into ordered stops.

    Coordinates drive the geometry; names/years are decoration. The three lists are
    supposed to align index-for-index, but institution names are comma-separated and a
    name containing a comma would desync them -- so names are only attached when the
    counts agree, and we never drop a coordinate just because a label is missing.
    """
    coords = _parse_edu_coords(coords_raw)
    if not coords:
        return []
    locs = _parse_edu_locations(locs_raw)
    insts = _parse_edu_institutions(insts_raw)
    use_locs = len(locs) == len(coords)
    use_insts = len(insts) == len(coords)
    stops = []
    for i, (lat, lon) in enumerate(coords):
        loc = locs[i] if use_locs else {}
        stops.append({
            'lat': lat, 'lon': lon,
            'inst': insts[i] if use_insts else '',
            'town': _clean_token(loc.get('town')),
            'country': _clean_token(loc.get('country')),
            'start': _clean_token(loc.get('start')),
            'end': _clean_token(loc.get('end')),
        })
    return stops


def _inst_key(name, lat, lon):
    """Group education stops by INSTITUTION, not by coordinate. One geocode routinely
    covers many distinct schools -- 18 share the Paris city-centre point -- so keying
    on coordinates alone would merge them into a single meaningless dot.

    Two kinds of stop carry no institution: the explicit "Not Specified" sentinel, and
    a blank. Both mean "studied in this city, school unrecorded", so both key by
    COORDINATE: one pooled bubble per city, sitting alongside that city's named
    institutions. Keying them by name instead would collapse ~100 cities worldwide
    into one dot, since they all share the string "Not Specified"."""
    n = ' '.join((name or '').split()).strip().lower()
    if n and n != NOT_SPECIFIED:
        return 'name:' + n
    return 'unspec:%.5f,%.5f' % (lat, lon)


def build_education_registry(stop_lists):
    """Assign every institution one stable, plotted coordinate.

    Two problems to fix:
      1. A name can appear at slightly different coordinates across rows -> take the
         most frequently recorded one, so an institution is one dot, not several.
      2. Distinct institutions frequently share one coordinate -> fan them out on a
         small deterministic ring so each is separately visible and clickable. The
         ring grows with the number of institutions sharing the point.

    Returns {key: {'name': display, 'lat': float, 'lon': float}}.
    """
    from collections import Counter, defaultdict

    coords_for = defaultdict(Counter)      # key -> Counter[(lat, lon)]
    names_for = defaultdict(Counter)       # key -> Counter[original spelling]
    for stops in stop_lists:
        for st in stops:
            k = _inst_key(st.get('inst'), st['lat'], st['lon'])
            coords_for[k][(st['lat'], st['lon'])] += 1
            nm = (st.get('inst') or '').strip()
            if nm:
                names_for[k][nm] += 1

    # canonical coordinate per institution = the most frequently recorded one
    canon = {k: c.most_common(1)[0][0] for k, c in coords_for.items()}

    # institutions sharing a canonical coordinate get fanned onto a ring
    shared = defaultdict(list)
    for k, (lat, lon) in canon.items():
        shared[(round(lat, 5), round(lon, 5))].append(k)

    registry = {}
    for (rlat, rlon), keys in shared.items():
        keys = sorted(keys)                     # deterministic placement
        n = len(keys)
        base_lat, base_lon = canon[keys[0]]
        if n == 1:
            k = keys[0]
            registry[k] = {'name': (names_for[k].most_common(1)[0][0] if names_for[k] else ''),
                           'lat': base_lat, 'lon': base_lon}
            continue
        # ring radius grows with crowding so the dots stay individually clickable
        radius = 0.0008 + 0.00022 * n
        lon_scale = 1.0 / max(math.cos(math.radians(base_lat)), 0.05)
        for i, k in enumerate(keys):
            theta = 2 * math.pi * i / n
            registry[k] = {
                'name': (names_for[k].most_common(1)[0][0] if names_for[k] else ''),
                'lat': base_lat + radius * math.sin(theta),
                'lon': base_lon + radius * math.cos(theta) * lon_scale,
            }
    return registry


# ============================ DATA LAYER ============================
def build_data_layer(csv_path):
    """Read + clean the CSV and return a pure-data dict (no rendering)."""
    print("Loading and cleaning data...")
    df = pd.read_csv(csv_path)

    for c in ['Born_Latitude', 'Born_Longitude', 'Died_Latitude', 'Died_Longitude']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['Born_Year_Clean'] = pd.to_numeric(df['Born_Year'], errors='coerce')
    df['Died_Year_Clean'] = pd.to_numeric(df['Died_Year'], errors='coerce')

    clean_df = df.dropna(subset=['Born_Latitude', 'Born_Longitude']).copy()

    norm_lookup = {_norm(c): c for c in clean_df.columns}
    actual_cols = {}
    all_specs = (FILTER_CATEGORIES + NOTE_CATEGORIES + QUALITY_COLUMNS +
                 [('education_coordinates', EDUCATION_COORDS_COL, 'Education Coordinates'),
                  ('education_locations', EDUCATION_LOCS_COL, 'Education Locations')])
    for internal_key, expected_col, _label in all_specs:
        matched = None
        candidates = [expected_col] + COLUMN_ALIASES.get(internal_key, [])
        # 1) exact, then 2) case/underscore-insensitive, across the name and its aliases
        for cand in candidates:
            if cand in clean_df.columns:
                matched = cand
                break
            if _norm(cand) in norm_lookup:
                matched = norm_lookup[_norm(cand)]
                break
        # 3) last-resort substring scan. Never let a non-notes key bind to a *_notes
        # column (or vice versa): 'instruments' would otherwise match 'Instruments_Notes'.
        if not matched:
            key_is_notes = internal_key.endswith('_notes')
            tnorm = _norm(expected_col)
            for col in clean_df.columns:
                cn = _norm(col)
                if cn.endswith('notes') != key_is_notes:
                    continue
                if tnorm in cn or cn in tnorm:
                    matched = col
                    break
        actual_cols[internal_key] = matched
        if not matched:
            print(f"-> note: no CSV column for '{internal_key}' (field will be blank)")

    def extract_unique_categories(dataframe, col):
        if not col or col not in dataframe.columns:
            return []
        items = set()
        for entry in dataframe[col].dropna().astype(str):
            if entry.lower() != 'nan' and entry.strip():
                for sub in entry.replace(';', ',').split(','):
                    sc = sub.strip()
                    if sc and sc.lower() not in ('nan', 'none', 'unknown'):
                        items.add(sc)
        return sorted(list(items))

    def get_clean_row_val(row_obj, internal_key):
        col = actual_cols.get(internal_key)
        if not col:
            return ''
        val = row_obj.get(col, '')
        if pd.isna(val) or str(val).lower().strip() == 'nan':
            return ''
        return str(val).strip()

    categories_data = {k: extract_unique_categories(clean_df, actual_cols[k])
                       for k, _c, _l in FILTER_CATEGORIES}
    category_meta = [{"key": k, "label": lbl} for k, _c, lbl in FILTER_CATEGORIES]
    note_meta = [{"key": k, "label": lbl} for k, _c, lbl in NOTE_CATEGORIES]

    value_label_maps = {}
    _wealth_opts = categories_data.get('father_is_wealthy', [])
    if _wealth_opts:
        value_label_maps['father_is_wealthy'] = {
            opt: WEALTH_DISPLAY.get(_norm_int_str(opt), opt) for opt in _wealth_opts}

    all_years = pd.concat([clean_df['Born_Year_Clean'], clean_df['Died_Year_Clean']]).dropna()
    min_year = int(all_years.min()) if not all_years.empty else 1700
    max_year = int(all_years.max()) if not all_years.empty else 2026

    # --- Coordinate layout -----------------------------------------------------
    # Points that share a coordinate render exactly on top of one another, making all
    # but the topmost unclickable. Three mechanisms, applied in order:
    #
    #   1. RINGS for hubs (>5 people at one place): fan them onto a circle.
    #   2. JITTER for everything else -- including singletons. The old build skipped
    #      groups of one, which is why a person born and buried in the same city had
    #      their birth and death dots perfectly stacked.
    #   3. BIRTH/DEATH SEPARATION: jitter alone is random, so two independent draws can
    #      still land close. Where a person's birth and death coordinates are identical,
    #      push them a fixed distance in exactly opposite directions.
    #
    # Longitude offsets are divided by cos(latitude) so a given offset covers roughly
    # the same ground distance at any latitude rather than collapsing near the poles.
    np.random.seed(42)
    print("Laying out coordinates (rings + jitter + birth/death separation)...")

    JITTER = 0.0010        # ~110 m: nudges coincident points apart
    SEPARATION = 0.0035    # ~390 m: birth vs death of the same person

    def _lon_scale(lat_series):
        return 1.0 / np.maximum(np.cos(np.radians(lat_series.astype(float))), 0.05)

    clean_df['born_center_lat'] = clean_df['Born_Latitude']
    clean_df['born_center_lon'] = clean_df['Born_Longitude']
    clean_df['died_center_lat'] = clean_df['Died_Latitude']
    clean_df['died_center_lon'] = clean_df['Died_Longitude']

    # --- births: ring the hubs, jitter everyone else ---
    clean_df['b_total'] = clean_df.groupby(['born_center_lat', 'born_center_lon'])['born_center_lat'].transform('count')
    clean_df['b_index'] = clean_df.groupby(['born_center_lat', 'born_center_lon']).cumcount()
    b_ring = clean_df['b_total'] > 5
    ang = 2 * np.pi * clean_df.loc[b_ring, 'b_index'] / clean_df.loc[b_ring, 'b_total']
    clean_df.loc[b_ring, 'Born_Latitude'] = clean_df.loc[b_ring, 'Born_Latitude'] + 0.01 * np.sin(ang)
    clean_df.loc[b_ring, 'Born_Longitude'] = clean_df.loc[b_ring, 'Born_Longitude'] + 0.01 * np.cos(ang)

    b_jit = ~b_ring                                   # every non-ring birth, singletons included
    n = int(b_jit.sum())
    if n:
        clean_df.loc[b_jit, 'Born_Latitude'] = clean_df.loc[b_jit, 'Born_Latitude'] + np.random.uniform(-JITTER, JITTER, size=n)
        clean_df.loc[b_jit, 'Born_Longitude'] = clean_df.loc[b_jit, 'Born_Longitude'] + \
            np.random.uniform(-JITTER, JITTER, size=n) * _lon_scale(clean_df.loc[b_jit, 'born_center_lat'])

    # --- deaths: same treatment, over the rows that actually have death coords ---
    vdm = clean_df['died_center_lat'].notna() & clean_df['died_center_lon'].notna()
    clean_df['d_total'] = 0
    clean_df['d_index'] = 0
    clean_df.loc[vdm, 'd_total'] = clean_df[vdm].groupby(['died_center_lat', 'died_center_lon'])['died_center_lat'].transform('count')
    clean_df.loc[vdm, 'd_index'] = clean_df[vdm].groupby(['died_center_lat', 'died_center_lon']).cumcount()
    d_ring = vdm & (clean_df['d_total'] > 5)
    ang2 = 2 * np.pi * clean_df.loc[d_ring, 'd_index'] / clean_df.loc[d_ring, 'd_total']
    clean_df.loc[d_ring, 'Died_Latitude'] = clean_df.loc[d_ring, 'Died_Latitude'] + 0.018 * np.sin(ang2)
    clean_df.loc[d_ring, 'Died_Longitude'] = clean_df.loc[d_ring, 'Died_Longitude'] + 0.018 * np.cos(ang2)

    d_jit = vdm & (~d_ring)
    n = int(d_jit.sum())
    if n:
        clean_df.loc[d_jit, 'Died_Latitude'] = clean_df.loc[d_jit, 'Died_Latitude'] + np.random.uniform(-JITTER, JITTER, size=n)
        clean_df.loc[d_jit, 'Died_Longitude'] = clean_df.loc[d_jit, 'Died_Longitude'] + \
            np.random.uniform(-JITTER, JITTER, size=n) * _lon_scale(clean_df.loc[d_jit, 'died_center_lat'])

    # --- born and died in the same place: force them apart along opposite bearings ---
    coincident = (vdm
                  & (clean_df['born_center_lat'] == clean_df['died_center_lat'])
                  & (clean_df['born_center_lon'] == clean_df['died_center_lon']))
    n = int(coincident.sum())
    if n:
        theta = np.random.uniform(0, 2 * np.pi, size=n)
        lon_s = _lon_scale(clean_df.loc[coincident, 'born_center_lat'])
        dlat = SEPARATION * np.sin(theta)
        dlon = SEPARATION * np.cos(theta) * lon_s
        clean_df.loc[coincident, 'Born_Latitude'] = clean_df.loc[coincident, 'Born_Latitude'] + dlat
        clean_df.loc[coincident, 'Born_Longitude'] = clean_df.loc[coincident, 'Born_Longitude'] + dlon
        clean_df.loc[coincident, 'Died_Latitude'] = clean_df.loc[coincident, 'Died_Latitude'] - dlat
        clean_df.loc[coincident, 'Died_Longitude'] = clean_df.loc[coincident, 'Died_Longitude'] - dlon
        print(f"-> separated {n} people born and died at the same coordinates")

    # --- education registry: one dot per INSTITUTION, with shared geocodes fanned out ---
    def _row_stops(row):
        return build_education_stops(
            row.get(actual_cols.get('education_coordinates')) if actual_cols.get('education_coordinates') else None,
            row.get(actual_cols.get('education_locations')) if actual_cols.get('education_locations') else None,
            row.get(actual_cols.get('education_institution')) if actual_cols.get('education_institution') else None)

    edu_registry = build_education_registry(_row_stops(r) for _i, r in clean_df.iterrows())
    print(f"-> {len(edu_registry)} distinct education institutions")

    def _attach_registry(stops):
        out = []
        for st in stops:
            k = _inst_key(st.get('inst'), st['lat'], st['lon'])
            reg = edu_registry.get(k)
            st = dict(st)
            st['key'] = k
            # rlat/rlon keep the TRUE geocode. The dots and the life vector use the
            # fanned-out plotted position, but the heatmap must stay a map of real
            # density, so it reads the raw coordinate instead.
            st['rlat'], st['rlon'] = st['lat'], st['lon']
            if reg:
                st['lat'], st['lon'] = reg['lat'], reg['lon']   # the plotted position
                if reg['name']:
                    st['inst'] = reg['name']                    # canonical spelling
            out.append(st)
        return out

    print("Building people records...")
    people = []
    conn_index_by_dfidx = {}
    for idx, row in clean_df.iterrows():
        first = str(row.get('First_Name', '')).replace('nan', '').strip()
        last = str(row.get('Last_Name', '')).replace('nan', '').strip()
        name = format_name_display(first, last)
        born_town = str(row.get('Born_Town', 'Unknown')).replace('nan', 'Unknown')
        died_town = str(row.get('Died_Town', 'Unknown')).replace('nan', 'Unknown')
        by = int(row['Born_Year_Clean']) if pd.notna(row['Born_Year_Clean']) else None
        dy = int(row['Died_Year_Clean']) if pd.notna(row['Died_Year_Clean']) else None
        has_death = bool(pd.notna(row['Died_Latitude']) and pd.notna(row['Died_Longitude']))
        sources_val = row.get('Sources_List', '')
        sources_text = '' if pd.isna(sources_val) else str(sources_val).strip()

        person = {
            'idx': len(people),
            'name': name,
            # The names a reader is likely to type. Half the catalog carries at least
            # one, and 66 figures are otherwise unreachable by the name they are known
            # by -- nobody searches for "Pierre Abailard" when they mean Peter Abelard.
            'alt': parse_alt_names(row.get(ALT_NAME_COL), name),
            'born_year': by, 'died_year': dy,
            'born_town': born_town, 'died_town': died_town,
            'blon': float(row['Born_Longitude']), 'blat': float(row['Born_Latitude']),
            'dlon': float(row['Died_Longitude']) if has_death else None,
            'dlat': float(row['Died_Latitude']) if has_death else None,
            'sources': sources_text,
            # Ordered education stops: geometry for the Education layer, and the
            # waypoints the life vector routes through between birth and death.
            # Each stop is snapped to its institution's registry coordinate, so every
            # mention of one school lands on exactly one dot.
            'edu': _attach_registry(_row_stops(row)),
        }
        for k, _c, _l in FILTER_CATEGORIES:
            person[k] = get_clean_row_val(row, k)
        for k, _c, _l in NOTE_CATEGORIES:
            person[k] = get_clean_row_val(row, k)
        people.append(person)
        conn_index_by_dfidx[idx] = person['idx']

    def _plain(v):
        s = '' if v is None else str(v).strip()
        return '' if s.lower() in ('', 'nan', 'none') else s

    def _wealth_label(raw):
        s = _plain(raw)
        return WEALTH_DISPLAY.get(_norm_int_str(s), '') if s else ''

    data_records = []
    for _idx, row in df.iterrows():
        first = str(row.get('First_Name', '')).replace('nan', '').strip()
        last = str(row.get('Last_Name', '')).replace('nan', '').strip()
        by = int(row['Born_Year_Clean']) if pd.notna(row['Born_Year_Clean']) else None
        dy = int(row['Died_Year_Clean']) if pd.notna(row['Died_Year_Clean']) else None
        _disp = format_name_display(first, last)
        rec = {
            'name': format_name_last_first(first, last),
            'alt_name': ', '.join(parse_alt_names(row.get(ALT_NAME_COL), _disp)),
            'born_year': by,
            'born_town': _plain(row.get('Born_Town')),
            'died_year': dy,
            'died_town': _plain(row.get('Died_Town')),
            'lifespan': (dy - by) if (by is not None and dy is not None and dy >= by) else None,
            'father_is_wealthy': _wealth_label(
                row.get(actual_cols['father_is_wealthy']) if actual_cols.get('father_is_wealthy') else None),
        }
        for _key, _c, _l in FILTER_CATEGORIES:
            if _key == 'father_is_wealthy':
                continue
            rec[_key] = get_clean_row_val(row, _key)
        for _key, _c, _l in NOTE_CATEGORIES:
            rec[_key] = get_clean_row_val(row, _key)

        # Data-quality signals. disagreement_count is numeric so the column sorts and
        # charts properly; a missing value stays None rather than becoming 0, which
        # would falsely read as "no disagreements".
        _dc = row.get(actual_cols['disagreement_count']) if actual_cols.get('disagreement_count') else None
        try:
            rec['disagreement_count'] = int(_dc) if pd.notna(_dc) and str(_dc).strip() != '' else None
        except (ValueError, TypeError):
            rec['disagreement_count'] = None
        rec['inconsistent_fields'] = get_clean_row_val(row, 'inconsistent_fields')
        rec['education_stops'] = len(build_education_stops(
            row.get(actual_cols.get('education_coordinates')) if actual_cols.get('education_coordinates') else None,
            row.get(actual_cols.get('education_locations')) if actual_cols.get('education_locations') else None,
            row.get(actual_cols.get('education_institution')) if actual_cols.get('education_institution') else None))
        rec['_map_index'] = conn_index_by_dfidx.get(_idx)
        data_records.append(rec)

    data_columns = [{'key': k, 'label': lbl, 'type': typ} for k, lbl, typ in DATA_TABLE_COLUMNS]
    data_filter_meta = [{'key': k, 'label': lbl} for k, _c, lbl in FILTER_CATEGORIES]
    data_category_options = {}
    for _key, _col, _lbl in FILTER_CATEGORIES:
        if _key == 'father_is_wealthy':
            opts = sorted({_wealth_label(v) for v in df[actual_cols[_key]].dropna()} - {''}) \
                if actual_cols.get(_key) else []
            data_category_options[_key] = opts
        else:
            data_category_options[_key] = extract_unique_categories(df, actual_cols[_key])

    _edu_people = sum(1 for p in people if p['edu'])
    _edu_stops = sum(len(p['edu']) for p in people)
    print(f"-> {len(people)} mapped people, {len(data_records)} catalog rows, timeline {min_year}-{max_year}")
    print(f"-> {_edu_stops} education stops across {_edu_people} people")

    return {
        'people': people,
        'categories': categories_data,
        'categoryMeta': category_meta,
        'noteMeta': note_meta,
        'valueLabelMaps': value_label_maps,
        'minYear': min_year,
        'maxYear': max_year,
        'data_records': data_records,
        'data_columns': data_columns,
        'data_filter_meta': data_filter_meta,
        'data_category_options': data_category_options,
    }


# ============ DATA TAB (carried over unchanged from the folium build) ============
# Standalone HTML/JS with no map-engine dependency. It talks to the map only via the
# global contract: window.filterMapData(), window.selectPerson(i), window.dashShowPage(),
# and the #query-master-dropzone pills -- all of which the MapLibre app still provides.
def _build_data_page(records, columns, filter_meta, category_options, value_label_maps):
    """Return the full HTML for the dashboard's Data tab: a sortable/filterable
    spreadsheet of the catalog plus a small custom chart builder. Everything runs
    client-side off the injected records, so it works on static hosting."""

    style = """<style>
    #page-data { overflow: hidden; }
    .dt-root { position:absolute; top:0; left:0; right:0; bottom:0; display:flex; flex-direction:column;
        font-family:"Helvetica Neue", Arial, sans-serif; color:#2c3e50; background:#f4f6f8; }
    .dt-toolbar { flex:0 0 auto; display:flex; flex-wrap:wrap; align-items:center; gap:10px;
        padding:10px 16px; background:#fff; border-bottom:1px solid #e3e7eb; }
    .dt-title { font-size:15px; font-weight:700; }
    .dt-count { font-size:12px; font-weight:400; color:#7f8c8d; }
    .dt-search { flex:1 1 200px; max-width:320px; padding:6px 9px; border:1px solid #bdc3c7; border-radius:4px; font-size:12px; }
    .dt-num { width:62px; padding:5px 6px; border:1px solid #bdc3c7; border-radius:4px; font-size:12px; }
    .dt-yearrange, .dt-mode { font-size:12px; color:#34495e; display:flex; align-items:center; gap:5px; }
    .dt-btn { padding:6px 12px; font-size:12px; font-weight:600; border:none; border-radius:4px; cursor:pointer; color:#fff; }
    .dt-btn-grey { background:#95a5a6; } .dt-btn-blue { background:#3498db; } .dt-btn-green { background:#2ecc71; } .dt-btn-dark { background:#34495e; }
    .dt-btn:hover { filter:brightness(1.07); }
    .dt-filterbar { flex:0 0 auto; display:flex; flex-wrap:wrap; align-items:center; gap:8px;
        padding:8px 16px; background:#fbfcfd; border-bottom:1px solid #e3e7eb; }
    .dt-filterbar select { padding:5px 7px; border:1px solid #bdc3c7; border-radius:4px; font-size:12px; background:#fff; color:#34495e; max-width:220px; }
    .dt-hint { font-size:11px; color:#95a5a6; font-style:italic; }
    .dt-pills { display:flex; flex-wrap:wrap; gap:5px; }
    .dt-pill { display:inline-flex; align-items:center; background:#e67e22; color:#fff; padding:3px 8px;
        border-radius:4px; font-size:11px; font-weight:700; }
    .dt-pill .dt-x { cursor:pointer; margin-left:7px; }
    .dt-body { flex:1 1 auto; min-height:0; display:flex; }
    .dt-tablewrap { flex:1 1 auto; min-width:0; overflow:auto; background:#fff; }
    .dt-table { border-collapse:collapse; font-size:12px; width:max-content; min-width:100%; }
    .dt-table thead th { position:sticky; top:0; z-index:2; background:#1f2d3a; color:#fff; text-align:left;
        padding:8px 10px; font-weight:600; white-space:nowrap; cursor:pointer; user-select:none; border-right:1px solid #2c3e50; }
    .dt-table thead th:hover { background:#27384a; }
    .dt-table thead th .dt-arrow { margin-left:5px; opacity:0.55; font-size:10px; }
    /* Column reordering: a grip affordance, plus drag/drop states. */
    .dt-table thead th { position:sticky; }  /* (already sticky; keeps specificity for the rules below) */
    .dt-table thead th .dt-grip { opacity:0; margin-right:4px; color:#8fa3b5; cursor:grab; font-size:11px; }
    .dt-table thead th:hover .dt-grip { opacity:0.7; }
    .dt-table thead th.dt-dragging { opacity:0.45; background:#27384a; }
    /* a left border marks where the dragged column will land (it inserts BEFORE the target) */
    .dt-table thead th.dt-drop-target { box-shadow: inset 3px 0 0 #2ecc71; }
    .dt-table tbody td { padding:6px 10px; border-bottom:1px solid #eef1f3; border-right:1px solid #f2f4f6;
        max-width:230px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .dt-table tbody td.dt-numcell { text-align:right; font-variant-numeric:tabular-nums; }
    .dt-table tbody tr:nth-child(even) { background:#fafbfc; }
    .dt-table tbody tr:hover { background:#eef7f1; }
    .dt-chartpanel { flex:0 0 360px; border-left:1px solid #e3e7eb; background:#fff; overflow:auto;
        padding:14px; display:flex; flex-direction:column; gap:9px; }
    .dt-chartpanel.dt-hidden { display:none; }
    .dt-section-title { font-size:13px; font-weight:700; color:#2c3e50; border-bottom:1px solid #eee; padding-bottom:5px; }
    .dt-chart-row { display:flex; align-items:center; justify-content:space-between; gap:8px; font-size:12px; }
    .dt-chart-row label { color:#34495e; font-weight:600; white-space:nowrap; }
    .dt-chart-row select, .dt-chart-row input { padding:5px 7px; border:1px solid #bdc3c7; border-radius:4px;
        font-size:12px; flex:1 1 auto; max-width:210px; }
    .dt-chart-canvas-wrap { position:relative; flex:1 1 auto; min-height:260px; margin-top:4px; }
    .dt-chart-empty { color:#95a5a6; font-size:12px; text-align:center; margin-top:30px; }
    .dt-yearrange select { padding:5px 6px; border:1px solid #bdc3c7; border-radius:4px; font-size:12px; background:#fff; color:#34495e; }
    .dt-stats { flex:0 0 auto; display:flex; flex-wrap:wrap; gap:10px; padding:8px 16px; background:#fff; border-bottom:1px solid #e3e7eb; }
    .dt-stat { background:#f4f6f8; border:1px solid #e3e7eb; border-radius:5px; padding:4px 11px; font-size:12px; color:#34495e; }
    .dt-stat b { color:#2c3e50; }
    .dt-stat-label { color:#7f8c8d; text-transform:uppercase; font-size:9px; letter-spacing:0.5px; display:block; margin-bottom:1px; }
    .dt-table tbody tr { cursor:pointer; }
    .dt-modal { position:absolute; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,0.45);
        display:flex; align-items:center; justify-content:center; z-index:50; }
    .dt-modal-box { background:#fff; width:560px; max-width:92%; max-height:82%; display:flex; flex-direction:column;
        border-radius:6px; box-shadow:0 8px 30px rgba(0,0,0,0.32); }
    .dt-modal-head { display:flex; align-items:center; justify-content:space-between; padding:12px 16px; border-bottom:1px solid #eee; }
    .dt-modal-head span:first-child { font-size:15px; font-weight:700; color:#2c3e50; }
    .dt-modal-close { cursor:pointer; font-size:22px; line-height:1; color:#7f8c8d; }
    .dt-modal-close:hover { color:#2c3e50; }
    .dt-modal-body { padding:6px 16px 16px; overflow:auto; }
    .dt-detail-row { display:flex; gap:12px; padding:7px 0; border-bottom:1px solid #f2f4f6; font-size:12.5px; }
    .dt-detail-k { flex:0 0 160px; color:#7f8c8d; font-weight:600; }
    .dt-detail-v { flex:1 1 auto; white-space:pre-wrap; word-break:break-word; color:#2c3e50; }
    /* Data-quality badge: how much the sources disagreed about this record. */
    .dt-quality { display:flex; align-items:center; gap:8px; flex-wrap:wrap;
        margin:10px 0 4px; padding:9px 12px; border-radius:6px; font-size:12px; }
    .dt-quality-clean { background:#eafaf1; border:1px solid #a9dfbf; color:#1e6b40; }
    .dt-quality-warn  { background:#fef5e7; border:1px solid #f5cba7; color:#8a5000; }
    .dt-quality-bad   { background:#fdedec; border:1px solid #f5b7b1; color:#922b21; }
    .dt-quality-none  { background:#f4f6f8; border:1px solid #e3e7eb; color:#7f8c8d; }
    .dt-qbadge { font-weight:700; padding:2px 8px; border-radius:10px; background:rgba(255,255,255,0.65); }
    .dt-qfields { font-size:11.5px; opacity:0.95; }
    .dt-flag { color:#b9770e; font-weight:700; }
    </style>"""

    markup = """<div class="dt-root">
      <div class="dt-toolbar">
        <span class="dt-title">Data Explorer</span><span class="dt-count" id="dt-count"></span>
        <input id="dt-search" class="dt-search" type="text" placeholder="Search all columns&hellip;">
        <span class="dt-yearrange">
          <select id="dt-yearfield"><option value="born_year">Born year</option><option value="died_year">Died year</option></select>
          <input id="dt-ymin" class="dt-num" type="number" placeholder="min"> &ndash; <input id="dt-ymax" class="dt-num" type="number" placeholder="max"></span>
        <span class="dt-mode">Match
          <label><input type="radio" name="dt-mode" value="and" checked> ALL</label>
          <label><input type="radio" name="dt-mode" value="or"> ANY</label>
        </span>
        <button id="dt-clear" class="dt-btn dt-btn-grey">Clear</button>
        <button id="dt-export" class="dt-btn dt-btn-blue">&#11015; Export filtered (CSV)</button>
        <button id="dt-toggle-charts" class="dt-btn dt-btn-dark">Hide charts</button>
      </div>
      <div class="dt-filterbar">
        <select id="dt-cat"></select>
        <select id="dt-val" disabled><option value="">value&hellip;</option></select>
        <span class="dt-hint">pick a value to add a filter &middot; click any row to see the full record</span>
        <div class="dt-pills" id="dt-pills"></div>
      </div>
      <div class="dt-stats" id="dt-stats"></div>
      <div class="dt-body">
        <div class="dt-tablewrap">
          <table class="dt-table"><thead id="dt-thead"></thead><tbody id="dt-tbody"></tbody></table>
        </div>
        <div class="dt-chartpanel" id="dt-chartpanel">
          <div class="dt-section-title">Chart builder</div>
          <div class="dt-chart-row"><label>Type</label>
            <select id="dt-ctype">
              <option value="bar">Bar</option>
              <option value="line">Line</option>
              <option value="area">Area</option>
              <option value="pie">Pie</option>
              <option value="doughnut">Doughnut</option>
              <option value="hist">Histogram</option>
              <option value="scatter">Scatter</option>
            </select></div>
          <div class="dt-chart-row" id="dt-row-dim"><label id="dt-lbl-dim">Group by</label><select id="dt-cdim"></select></div>
          <div class="dt-chart-row" id="dt-row-binsize"><label>Bin size</label><select id="dt-cbinsize"></select></div>
          <div class="dt-chart-row" id="dt-row-split"><label>Split by</label><select id="dt-csplit"></select></div>
          <div class="dt-chart-row" id="dt-row-measure"><label>Measure</label><select id="dt-cmeasure"></select></div>
          <div class="dt-chart-row" id="dt-row-mfield"><label>of field</label><select id="dt-cmfield"></select></div>
          <div class="dt-chart-row" id="dt-row-y"><label>Y axis</label><select id="dt-cy"></select></div>
          <div class="dt-chart-row" id="dt-row-color"><label>Color by</label><select id="dt-ccolor"></select></div>
          <div class="dt-chart-row" id="dt-row-sort"><label>Sort</label>
            <select id="dt-csort"><option value="val_desc">Value &darr;</option><option value="val_asc">Value &uarr;</option><option value="label">Label A&ndash;Z</option></select></div>
          <div class="dt-chart-row" id="dt-row-topn"><label>Max groups</label><input id="dt-ctopn" type="number" value="15" min="1" max="80"></div>
          <div class="dt-chart-row" id="dt-row-other"><label>Remainder as &ldquo;Other&rdquo;</label><input id="dt-cother" type="checkbox"></div>
          <div class="dt-chart-row" id="dt-row-nounk"><label>Exclude Unknown</label><input id="dt-cnounk" type="checkbox"></div>
          <div class="dt-chart-row" id="dt-row-bins"><label>Bins</label><input id="dt-cbins" type="number" value="20" min="2" max="100"></div>
          <div class="dt-chart-row" id="dt-row-orient"><label>Horizontal</label><input id="dt-choriz" type="checkbox"></div>
          <div class="dt-chart-row" id="dt-row-stacked"><label>Stacked</label><input id="dt-cstacked" type="checkbox"></div>
          <div style="display:flex; gap:8px; margin-top:2px;">
            <button id="dt-cbuild" class="dt-btn dt-btn-green" style="flex:1;">Build chart</button>
            <button id="dt-cpng" class="dt-btn dt-btn-blue" title="Download chart as PNG">&#11015; PNG</button>
          </div>
          <div class="dt-chart-canvas-wrap"><canvas id="dt-canvas"></canvas>
            <div class="dt-chart-empty" id="dt-chart-empty">Configure and build a chart from the filtered rows.</div></div>
        </div>
      </div>
      <div id="dt-modal" class="dt-modal" style="display:none;">
        <div class="dt-modal-box">
          <div class="dt-modal-head"><span id="dt-modal-title"></span>
            <span style="display:flex; align-items:center; gap:12px;">
              <button id="dt-modal-map" class="dt-btn dt-btn-green" style="font-size:11px; padding:5px 11px;">Show on map</button>
              <span id="dt-modal-close" class="dt-modal-close">&times;</span>
            </span></div>
          <div class="dt-modal-body" id="dt-modal-body"></div>
        </div>
      </div>
    </div>"""

    script = r"""<script>
    (function(){
      var RECORDS = __RECORDS__;
      var COLUMNS = __COLUMNS__;
      var FILTER_META = __FILTER_META__;
      var CAT_OPTIONS = __CAT_OPTIONS__;
      var VALUE_LABELS = __VALUE_LABELS__;   // {key: {rawValue: label}} for fields shown relabelled (e.g. Family Wealth)
      var YEAR_COLS = __YEAR_COLUMNS__;      // columns holding a year, not a quantity
      var UNKNOWN_TOKENS = __UNKNOWN_TOKENS__;  // values meaning "not recorded"
__FMT_YEAR__
      function isYearCol(key){ return YEAR_COLS.indexOf(key) !== -1; }

      // Inverse wealth map (label -> raw) so we can translate filters to/from the Map,
      // whose pills store the raw -1/0/1 value while the Data tab stores the label.
      var LABEL_TO_RAW = {};
      Object.keys(VALUE_LABELS).forEach(function(k){
        LABEL_TO_RAW[k] = {}; var m = VALUE_LABELS[k];
        Object.keys(m).forEach(function(raw){ LABEL_TO_RAW[k][m[raw]] = raw; });
      });
      var detailMapIndex = null;             // connections index of the person shown in the modal
      var lastSyncSig = null;                // guard against map/data filter sync loops

      var colByKey = {}; COLUMNS.forEach(function(c){ colByKey[c.key] = c; });
      var numericKeys = COLUMNS.filter(function(c){ return c.type === 'number'; }).map(function(c){ return c.key; });

      var sortKey = 'name', sortDir = 1;   // 1 asc, -1 desc
      var pills = [];                       // {key, value}
      var chart = null;

      function el(id){ return document.getElementById(id); }
      function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
      function splitTokens(v){ return String(v==null?'':v).split(/[,;]/).map(function(s){ return s.trim(); }).filter(Boolean); }
      function lcTokens(v){ return splitTokens(v).map(function(s){ return s.toLowerCase(); }); }

      // ---------- filtering (mirrors the map: category tokens + AND/OR) ----------
      function currentFilters(){
        var modeEl = document.querySelector('input[name="dt-mode"]:checked');
        return { mode: modeEl ? modeEl.value : 'and', q: (el('dt-search').value||'').trim().toLowerCase(),
                 yearField: el('dt-yearfield').value, ymin: parseFloat(el('dt-ymin').value), ymax: parseFloat(el('dt-ymax').value), pills: pills.slice() };
      }
      function rowMatches(r, f){
        if (f.q){
          var hay = COLUMNS.map(function(c){ return r[c.key]; }).join(' \u0001 ').toLowerCase();
          if (hay.indexOf(f.q) === -1) return false;
        }
        var yv = r[f.yearField];
        if (!isNaN(f.ymin) && (yv == null || yv < f.ymin)) return false;
        if (!isNaN(f.ymax) && (yv == null || yv > f.ymax)) return false;
        if (f.pills.length){
          var test = function(p){ return lcTokens(r[p.key]).indexOf(String(p.value).toLowerCase()) !== -1; };
          if (f.mode === 'or'){ if (!f.pills.some(test)) return false; }
          else { if (!f.pills.every(test)) return false; }
        }
        return true;
      }
      function filteredRows(){ var f = currentFilters(); return RECORDS.filter(function(r){ return rowMatches(r, f); }); }

      // ---------- sorting ----------
      function sortRows(rows){
        var col = colByKey[sortKey], num = col && col.type === 'number';
        return rows.slice().sort(function(a,b){
          var x = a[sortKey], y = b[sortKey];
          if (num){
            var ax = (x==null||isNaN(x)), ay = (y==null||isNaN(y));
            if (ax && ay) return 0; if (ax) return 1; if (ay) return -1;   // blanks last
            return (x - y) * sortDir;
          }
          var sx = String(x==null?'':x).toLowerCase(), sy = String(y==null?'':y).toLowerCase();
          if (sx === sy) return 0;
          if (sx === '') return 1; if (sy === '') return -1;               // blanks last
          return (sx < sy ? -1 : 1) * sortDir;
        });
      }

      // ---------- table ----------
      // Columns can be dragged into any order. COLUMNS is the single source of truth --
      // the header, every row, the search haystack, the detail modal and the CSV export
      // all map over it -- so reordering the array reorders all of them at once.
      var dragKey = null;   // key of the column currently being dragged

      function moveColumn(fromKey, toKey){
        if (!fromKey || fromKey === toKey) return;
        var from = COLUMNS.findIndex(function(c){ return c.key === fromKey; });
        var to = COLUMNS.findIndex(function(c){ return c.key === toKey; });
        if (from < 0 || to < 0) return;
        var moved = COLUMNS.splice(from, 1)[0];
        COLUMNS.splice(to, 0, moved);   // drop BEFORE the target column
        renderHead(); renderBody();
      }

      function renderHead(){
        var tr = COLUMNS.map(function(c){
          var mark = (c.key === sortKey) ? (sortDir > 0 ? '\u25B2' : '\u25BC') : '\u25B5';
          // draggable is on the th itself; the grip is a visual affordance only
          return '<th draggable="true" data-key="'+c.key+'" title="Click to sort \u00b7 drag to reorder">' +
                 '<span class="dt-grip" aria-hidden="true">\u205E</span>' +
                 esc(c.label)+'<span class="dt-arrow">'+mark+'</span></th>';
        }).join('');
        el('dt-thead').innerHTML = '<tr>'+tr+'</tr>';
        Array.prototype.forEach.call(el('dt-thead').querySelectorAll('th'), function(th){
          var key = th.getAttribute('data-key');

          // Click still sorts. A drag does not fire click, so the two do not collide;
          // the guard below also ignores the click that can follow a drop.
          th.onclick = function(){
            if (th.__justDropped){ th.__justDropped = false; return; }
            if (key === sortKey) sortDir = -sortDir; else { sortKey = key; sortDir = 1; }
            renderHead(); renderBody();
          };

          th.ondragstart = function(ev){
            dragKey = key;
            th.classList.add('dt-dragging');
            // Firefox will not start a drag without data set.
            try { ev.dataTransfer.setData('text/plain', key); ev.dataTransfer.effectAllowed = 'move'; } catch(e){}
          };
          th.ondragend = function(){ dragKey = null;
            Array.prototype.forEach.call(el('dt-thead').querySelectorAll('th'), function(x){
              x.classList.remove('dt-dragging','dt-drop-target'); }); };
          th.ondragover = function(ev){ if (dragKey && dragKey !== key){ ev.preventDefault();
            try { ev.dataTransfer.dropEffect = 'move'; } catch(e){} } };
          th.ondragenter = function(){ if (dragKey && dragKey !== key) th.classList.add('dt-drop-target'); };
          th.ondragleave = function(){ th.classList.remove('dt-drop-target'); };
          th.ondrop = function(ev){
            ev.preventDefault();
            th.classList.remove('dt-drop-target');
            var src = dragKey || (ev.dataTransfer ? ev.dataTransfer.getData('text/plain') : null);
            // renderHead rebuilds these th nodes, so mark the NEW target after the move
            moveColumn(src, key);
            var moved = el('dt-thead').querySelector('th[data-key="'+key+'"]');
            if (moved) moved.__justDropped = true;   // swallow the click that trails the drop
          };
        });
      }
      window.__moveColumn = moveColumn;   // exercised by the tests
      function renderBody(){
        var rows = sortRows(filteredRows());
        el('dt-tbody').innerHTML = rows.map(function(r, ri){
          return '<tr data-ri="'+ri+'">'+COLUMNS.map(function(c){
            var v = r[c.key], isNum = c.type === 'number';
            // Years display as "350 BC" but keep sorting on the raw negative underneath.
            // lifespan is a number too, and must NOT be treated as a year.
            var disp = (v==null || v==='') ? '' : esc(isYearCol(c.key) ? fmtYear(v) : v);
            return '<td class="'+(isNum?'dt-numcell':'')+'" title="'+esc(v==null?'':v)+'">'+disp+'</td>';
          }).join('')+'</tr>';
        }).join('');
        el('dt-count').textContent = 'showing ' + rows.length + ' of ' + RECORDS.length;
        window.__dtFiltered = rows;
        window.__dtRows = rows;
        updateStats(rows);
      }

      // Quick-orientation strip: people in view, born-year span, top disciplines.
      // Scientific Disciplines is one of the untested fields; when it is hidden the
      // "Top disciplines" stat is dropped rather than left showing a blank.
      var SHOW_DISCIPLINE_STAT = __SHOW_DISCIPLINE_STAT__;

      function updateStats(rows){
        var bys = rows.map(function(r){ return r.born_year; }).filter(function(v){ return v!=null && !isNaN(v); });
        var span = bys.length ? fmtYearRange(Math.min.apply(null, bys), Math.max.apply(null, bys)) : '\u2014';

        var html =
          '<div class="dt-stat"><span class="dt-stat-label">People</span><b>' + rows.length + '</b> of ' + RECORDS.length + '</div>' +
          '<div class="dt-stat"><span class="dt-stat-label">Born span</span><b>' + span + '</b></div>';

        if (SHOW_DISCIPLINE_STAT){
          var disc = {};
          rows.forEach(function(r){ splitTokens(r.scientific_disciplines).forEach(function(d){ disc[d] = (disc[d]||0) + 1; }); });
          var top = Object.keys(disc).map(function(k){ return { k:k, n:disc[k] }; }).sort(function(a,b){ return b.n - a.n; }).slice(0, 3);
          var topStr = top.length ? top.map(function(t){ return esc(t.k) + ' (' + t.n + ')'; }).join(', ') : '\u2014';
          html += '<div class="dt-stat"><span class="dt-stat-label">Top disciplines</span>' + topStr + '</div>';
        }
        el('dt-stats').innerHTML = html;
      }

      // ---------- record detail modal (see the full text of any row) ----------
      // A short verdict on how trustworthy the row is, from the two quality columns.
      function qualityHtml(rec){
        var n = rec.disagreement_count;
        var fields = (rec.inconsistent_fields || '').trim();
        if (n == null || isNaN(n)){
          return '<div class="dt-quality dt-quality-none">No data-quality information recorded for this entry.</div>';
        }
        var cls = (n === 0) ? 'dt-quality-clean' : (n <= 2 ? 'dt-quality-warn' : 'dt-quality-bad');
        var word = (n === 1) ? 'source disagreement' : 'source disagreements';
        var s = '<div class="dt-quality ' + cls + '">' +
                '<span class="dt-qbadge">' + n + '</span><span>' + word + '</span>';
        if (fields){
          s += '<span class="dt-qfields">&middot; inconsistent: ' + esc(fields) + '</span>';
        } else if (n === 0){
          s += '<span class="dt-qfields">&middot; no inconsistent fields</span>';
        }
        return s + '</div>';
      }

      function openDetail(rec){
        if (!rec) return;
        el('dt-modal-title').textContent = rec.name || '(unnamed)';
        el('dt-modal-body').innerHTML = qualityHtml(rec) + COLUMNS.map(function(c){
          var v = rec[c.key];
          var disp = (v==null || v==='') ? '\u2014' : esc(isYearCol(c.key) ? fmtYear(v) : v);
          return '<div class="dt-detail-row"><div class="dt-detail-k">'+esc(c.label)+'</div><div class="dt-detail-v">'+disp+'</div></div>';
        }).join('');
        detailMapIndex = (rec._map_index != null) ? rec._map_index : null;
        var mb = el('dt-modal-map');
        if (detailMapIndex != null){ mb.disabled = false; mb.textContent = 'Show on map'; mb.style.opacity = '1'; mb.style.cursor = 'pointer'; mb.title = ''; }
        else { mb.disabled = true; mb.textContent = 'Not on map'; mb.style.opacity = '0.5'; mb.style.cursor = 'default'; mb.title = 'This person has no birth coordinates, so no map marker.'; }
        el('dt-modal').style.display = 'flex';
      }
      function closeDetail(){ el('dt-modal').style.display = 'none'; }

      // Jump to the Map tab and highlight the person from the detail modal.
      function showOnMap(){
        if (detailMapIndex == null) return;
        var mi = detailMapIndex;
        closeDetail();
        var mapTab = document.querySelector('#dash-nav .dash-tab[data-page="page-map"]');
        if (window.dashShowPage && mapTab) window.dashShowPage('page-map', mapTab);
        setTimeout(function(){ try { if (window.selectPerson) window.selectPerson(mi); } catch(e){} }, 120);
      }

      // ---------- shared category filters (Data <-> Map) ----------
      // Only the category pills + AND/OR mode are shared; the Map's timeline and the
      // Data tab's year-range/search stay local. The Map stores Family Wealth as the
      // raw -1/0/1 code while the Data tab stores the label, so we translate at the
      // boundary. lastSyncSig stops the two sides from echoing each other in a loop.
      function mapDropzone(){ return document.getElementById('query-master-dropzone'); }
      function catLabel(key){ var m = FILTER_META.filter(function(x){ return x.key===key; })[0]; return m ? m.label : key; }
      function rawForMap(key, value){ return (LABEL_TO_RAW[key] && LABEL_TO_RAW[key][value] != null) ? LABEL_TO_RAW[key][value] : value; }
      function labelForData(key, raw){ return (VALUE_LABELS[key] && VALUE_LABELS[key][raw] != null) ? VALUE_LABELS[key][raw] : raw; }
      function canonicalSig(list, mode){ return (mode||'and') + '|' + list.map(function(p){ return p.key+'='+p.value; }).slice().sort().join('&'); }
      function dataMode(){ return (document.querySelector('input[name="dt-mode"]:checked')||{}).value || 'and'; }

      function pushDataToMap(){
        var dz = mapDropzone(); if (!dz) return;                 // map panel not built yet
        var sig = canonicalSig(pills, dataMode());
        if (sig === lastSyncSig) return; lastSyncSig = sig;
        Array.prototype.forEach.call(dz.querySelectorAll('.query-pill'), function(n){ n.remove(); });
        var ph = document.getElementById('query-drop-placeholder');
        pills.forEach(function(p){
          var pill = document.createElement('div');
          pill.className = 'query-pill';
          pill.setAttribute('data-category', p.key);
          pill.setAttribute('data-value', rawForMap(p.key, p.value));
          pill.style.background = '#e67e22'; pill.style.borderColor = '#d35400'; pill.style.cursor = 'default'; pill.draggable = false;
          pill.textContent = catLabel(p.key) + ': ' + p.value;
          var x = document.createElement('span'); x.innerHTML = ' &times;'; x.style.cssText = 'cursor:pointer;color:#fff;margin-left:7px;';
          x.onclick = function(){ pill.remove(); if (dz.querySelectorAll('.query-pill').length === 0 && ph) ph.style.display = 'block'; };
          pill.appendChild(x); dz.appendChild(pill);
        });
        if (ph) ph.style.display = pills.length ? 'none' : 'block';
        var mr = document.querySelector('input[name="query-match-mode"][value="'+dataMode()+'"]'); if (mr) mr.checked = true;
        if (window.filterMapData) window.filterMapData();
      }

      function pullFromMapToData(){
        var dz = mapDropzone(); if (!dz) return;
        var mapPills = Array.prototype.map.call(dz.querySelectorAll('.query-pill'), function(n){
          var k = n.getAttribute('data-category');
          return { key: k, value: labelForData(k, n.getAttribute('data-value')) };
        });
        var mmode = (document.querySelector('input[name="query-match-mode"]:checked')||{}).value || 'and';
        var sig = canonicalSig(mapPills, mmode);
        if (sig === lastSyncSig) return; lastSyncSig = sig;
        pills = mapPills;
        var dr = document.querySelector('input[name="dt-mode"][value="'+mmode+'"]'); if (dr) dr.checked = true;
        refreshPills(); renderBody();
      }

      // ---------- pill filter builder ----------
      function refreshPills(){
        el('dt-pills').innerHTML = pills.map(function(p, i){
          var meta = FILTER_META.filter(function(m){ return m.key === p.key; })[0];
          var lbl = meta ? meta.label : p.key;
          return '<span class="dt-pill">'+esc(lbl)+': '+esc(p.value)+'<span class="dt-x" data-i="'+i+'">\u00d7</span></span>';
        }).join('');
        Array.prototype.forEach.call(el('dt-pills').querySelectorAll('.dt-x'), function(x){
          x.onclick = function(){ pills.splice(parseInt(x.getAttribute('data-i')), 1); refreshPills(); renderBody(); pushDataToMap(); };
        });
      }
      function initCatSelect(){
        var cat = el('dt-cat'), val = el('dt-val');
        cat.innerHTML = '<option value="">+ Add filter: category\u2026</option>' +
          FILTER_META.map(function(m){ return '<option value="'+m.key+'">'+esc(m.label)+'</option>'; }).join('');
        cat.onchange = function(){
          var key = cat.value;
          if (!key){ val.innerHTML = '<option value="">value\u2026</option>'; val.disabled = true; return; }
          var opts = CAT_OPTIONS[key] || [];
          val.innerHTML = '<option value="">value\u2026</option>' +
            opts.map(function(o){ return '<option value="'+esc(o)+'">'+esc(o)+'</option>'; }).join('');
          val.disabled = false;
        };
        val.onchange = function(){
          var key = cat.value, value = val.value;
          if (!key || !value) return;
          if (!pills.some(function(p){ return p.key===key && p.value===value; })) pills.push({ key:key, value:value });
          cat.value = ''; val.innerHTML = '<option value="">value\u2026</option>'; val.disabled = true;
          refreshPills(); renderBody(); pushDataToMap();
        };
      }

      // ---------- CSV export of the filtered rows ----------
      function exportCsv(){
        var rows = window.__dtFiltered || filteredRows();
        function field(v){ return '"' + String(v==null?'':v).replace(/"/g,'""') + '"'; }
        var lines = [ COLUMNS.map(function(c){ return field(c.label); }).join(',') ];
        rows.forEach(function(r){ lines.push(COLUMNS.map(function(c){ return field(r[c.key]); }).join(',')); });
        var csv = '\ufeff' + lines.join('\r\n');
        var blob = new Blob([csv], { type:'text/csv;charset=utf-8;' });
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a'); a.href = url; a.download = 'westfall_data_' + rows.length + 'rows.csv';
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
      }

      // ---------- chart builder ----------
      var PALETTE = ['#2ecc71','#3498db','#e67e22','#9b59b6','#e74c3c','#1abc9c','#f1c40f','#34495e','#16a085','#d35400','#8e44ad','#2980b9','#27ae60','#c0392b','#7f8c8d'];
      function round2(x){ return Math.round(x*100)/100; }
      function hexA(hex, a){ var n = parseInt(hex.slice(1),16); return 'rgba('+((n>>16)&255)+','+((n>>8)&255)+','+(n&255)+','+a+')'; }
      function fillSelect(sel, items){ sel.innerHTML = items.map(function(it){ return '<option value="'+it.v+'">'+esc(it.t)+'</option>'; }).join(''); }
      function catFields(){ return COLUMNS.filter(function(c){ return c.type==='cat' || c.type==='multi'; }); }

      function populateDim(){
        var t = el('dt-ctype').value, items = [], prev = el('dt-cdim').value;
        if (t === 'hist' || t === 'scatter'){
          numericKeys.forEach(function(k){ items.push({ v:k, t:colByKey[k].label }); });
        } else {
          catFields().forEach(function(c){ items.push({ v:c.key, t:c.label }); });
          numericKeys.forEach(function(k){ items.push({ v:k, t:colByKey[k].label }); });
        }
        fillSelect(el('dt-cdim'), items);
        for (var i=0;i<el('dt-cdim').options.length;i++){ if (el('dt-cdim').options[i].value===prev){ el('dt-cdim').selectedIndex=i; break; } }
      }

      function setupChartControls(){
        fillSelect(el('dt-cbinsize'), [{v:'1',t:'1'},{v:'5',t:'5'},{v:'10',t:'10'},{v:'25',t:'25'},{v:'50',t:'50'},{v:'100',t:'100'}]);
        el('dt-cbinsize').value = '10';
        fillSelect(el('dt-cmeasure'), [{v:'count',t:'Count'},{v:'avg',t:'Average'},{v:'median',t:'Median'},{v:'sum',t:'Sum'},{v:'min',t:'Min'},{v:'max',t:'Max'}]);
        fillSelect(el('dt-cmfield'), numericKeys.map(function(k){ return { v:k, t:colByKey[k].label }; }));
        fillSelect(el('dt-cy'), numericKeys.map(function(k){ return { v:k, t:colByKey[k].label }; }));
        var noneCat = [{v:'',t:'(none)'}].concat(catFields().map(function(c){ return { v:c.key, t:c.label }; }));
        fillSelect(el('dt-csplit'), noneCat);
        fillSelect(el('dt-ccolor'), noneCat);
        el('dt-ctype').onchange = updateChartRows;
        el('dt-cdim').onchange = updateChartRows;
        el('dt-cmeasure').onchange = updateChartRows;
        el('dt-csplit').onchange = updateChartRows;
        updateChartRows();
      }

      function updateChartRows(){
        populateDim();
        var t = el('dt-ctype').value;
        var dimCol = colByKey[el('dt-cdim').value];
        var dimNumeric = dimCol && dimCol.type==='number';
        var measured = (t==='bar'||t==='line'||t==='area'||t==='pie'||t==='doughnut');
        var measure = el('dt-cmeasure').value;
        var canSplit = (t==='bar'||t==='line'||t==='area');
        function show(id, on){ el(id).style.display = on ? 'flex' : 'none'; }
        show('dt-row-dim', true);
        show('dt-row-binsize', measured && dimNumeric);
        show('dt-row-split', canSplit);
        show('dt-row-measure', measured);
        show('dt-row-mfield', measured && measure!=='count');
        show('dt-row-y', t==='scatter');
        show('dt-row-color', t==='scatter');
        show('dt-row-sort', (t==='bar'||t==='pie'||t==='doughnut') && !dimNumeric);
        show('dt-row-topn', (t==='bar'||t==='pie'||t==='doughnut') && !dimNumeric);
        show('dt-row-other', (t==='bar'||t==='pie'||t==='doughnut') && !dimNumeric);
        // Applies to any chart with a categorical axis, a split, or a colour series --
        // a scatter coloured by religion has an Unknown series just as a pie does.
        show('dt-row-nounk', !dimNumeric || canSplit || t==='scatter');
        show('dt-row-bins', t==='hist');
        show('dt-row-orient', t==='bar');
        show('dt-row-stacked', (t==='bar'||t==='area') && !!el('dt-csplit').value);
        el('dt-lbl-dim').textContent = (t==='scatter') ? 'X axis' : (t==='hist') ? 'Field' : 'Group by';
      }

      // ---- aggregation helpers ----
      function binSize(){ var b = parseInt(el('dt-cbinsize').value); return (b && b>0) ? b : 10; }
      // Bin labels for a year axis. The "1640s" shorthand only reads correctly for
      // positive 4-digit years, so BC bins fall back to an explicit range.
      function rangeLabel(b, bs, isYear){
        b = parseInt(b);
        if (bs===1) return isYear ? fmtYear(b) : String(b);
        if (bs===10 && b>=1000) return b+'s';
        return isYear ? fmtYearRange(b, b+bs-1) : (b+'\u2013'+(b+bs-1));
      }
      function labelFor(lbl, dimKey){ return (colByKey[dimKey].type==='number') ? rangeLabel(lbl, binSize(), isYearCol(dimKey)) : lbl; }
      // Whole-value match, never a substring: "Unknown Mechanics" is a real discipline
      // and must survive. Numbers are never unknown-by-name -- a missing one is null,
      // and valuesOf already drops those.
      function isUnknownValue(v){
        return UNKNOWN_TOKENS.indexOf(String(v).trim().toLowerCase()) !== -1;
      }
      function excludeUnknown(){
        var c = el('dt-cnounk');
        return !!(c && c.checked);
      }

      // Every chart value passes through here -- group by, split by, colour by -- so
      // this is the one place the exclusion has to happen.
      function valuesOf(r, key){
        var col = colByKey[key];
        if (col.type==='number'){ var y = r[key]; if (y==null||isNaN(y)) return []; var bs = binSize(); return [String(Math.floor(y/bs)*bs)]; }
        var out;
        if (col.type==='multi') out = splitTokens(r[key]);
        else { var s = (r[key]==null||r[key]==='') ? '' : String(r[key]).trim(); out = s ? [s] : []; }
        if (excludeUnknown()) out = out.filter(function(v){ return !isUnknownValue(v); });
        return out;
      }
      function aggregate(measure, field, items){
        if (measure==='count') return items.length;
        var vals = items.map(function(r){ return r[field]; }).filter(function(v){ return v!=null && !isNaN(v); });
        if (!vals.length) return 0;
        if (measure==='sum') return vals.reduce(function(a,b){ return a+b; }, 0);
        if (measure==='avg') return vals.reduce(function(a,b){ return a+b; }, 0)/vals.length;
        if (measure==='min') return Math.min.apply(null, vals);
        if (measure==='max') return Math.max.apply(null, vals);
        if (measure==='median'){ vals.sort(function(a,b){ return a-b; }); var m=Math.floor(vals.length/2); return vals.length%2 ? vals[m] : (vals[m-1]+vals[m])/2; }
        return items.length;
      }
      function measureLbl(measure, field){ if (measure==='count') return 'Count'; var f=(colByKey[field]||{label:''}).label; return measure.charAt(0).toUpperCase()+measure.slice(1)+' '+f; }

      // ---- descriptive auto-titles ----
      function measureTitle(measure, field){
        var f = (colByKey[field]||{label:''}).label;
        if (measure==='count') return 'Count of people';
        if (measure==='avg') return 'Average ' + f;
        if (measure==='median') return 'Median ' + f;
        if (measure==='sum') return 'Total ' + f;
        if (measure==='min') return 'Minimum ' + f;
        if (measure==='max') return 'Maximum ' + f;
        return 'Count';
      }
      function dimTitle(dimKey){
        var c = colByKey[dimKey], lbl = c ? c.label : dimKey;
        if (c && c.type==='number'){ var bs = binSize();
          var suf = (bs===1) ? 'by year' : (bs===10) ? 'by decade' : (bs===100) ? 'by century' : ('in ' + bs + '-year bins');
          return lbl + ' (' + suf + ')';
        }
        return lbl;
      }
      function chartTitle(t){
        if (t==='hist'){ var fk = el('dt-cdim').value; if (!colByKey[fk] || colByKey[fk].type!=='number') fk = 'born_year';
          return 'Distribution of ' + colByKey[fk].label + ' (' + (parseInt(el('dt-cbins').value)||20) + ' bins)'; }
        if (t==='scatter'){ var xk = el('dt-cdim').value; if (!colByKey[xk] || colByKey[xk].type!=='number') xk = 'born_year';
          var yk = el('dt-cy').value, cb = el('dt-ccolor').value;
          var s = colByKey[yk].label + ' vs ' + colByKey[xk].label; if (cb) s += ', colored by ' + colByKey[cb].label; return s; }
        var dimKey = el('dt-cdim').value, measure = el('dt-cmeasure').value, mfield = el('dt-cmfield').value;
        var s2 = measureTitle(measure, mfield) + ' by ' + dimTitle(dimKey);
        var split = (t==='bar'||t==='line'||t==='area') ? el('dt-csplit').value : '';
        if (split) s2 += ', split by ' + colByKey[split].label;
        return s2;
      }
      function activeFilterSummary(n){
        var f = currentFilters(), parts = [];
        if (f.pills.length){
          var ps = f.pills.map(function(p){ var m = FILTER_META.filter(function(x){ return x.key===p.key; })[0]; return (m?m.label:p.key) + ': ' + p.value; });
          parts.push(ps.join(f.mode==='or' ? ' OR ' : '; '));
        }
        if (f.q) parts.push('search "' + f.q + '"');
        var yl = (colByKey[f.yearField]||{label:'Year'}).label;
        if (!isNaN(f.ymin) && !isNaN(f.ymax)) parts.push(yl + ' ' + fmtYearRange(f.ymin, f.ymax));
        else if (!isNaN(f.ymin)) parts.push(yl + ' \u2265 ' + fmtYear(f.ymin));
        else if (!isNaN(f.ymax)) parts.push(yl + ' \u2264 ' + fmtYear(f.ymax));
        var base = parts.length ? ('Filtered: ' + parts.join('  \u00b7  ')) : 'All records';
        base += '   (n = ' + n + ')';
        // A chart that drops "Unknown" has a different base from the one its filters
        // describe -- religion is Unknown for 62% of the catalog. The PNG travels
        // without this panel, so the chart has to carry the caveat itself.
        if (excludeUnknown()) base += '   \u00b7  Unknown excluded';
        return base;
      }
      function wrapLines(s, max){
        if (s.length <= max) return s;
        var words = s.split(' '), lines = [], cur = '';
        words.forEach(function(w){ if ((cur + ' ' + w).trim().length > max){ if (cur) lines.push(cur.trim()); cur = w; } else { cur = (cur + ' ' + w).trim(); } });
        if (cur) lines.push(cur.trim());
        return lines;
      }

      function buildSeries(rows, dimKey, splitKey, measure, mfield, sortMode, topN, useOther){
        var dimNumeric = colByKey[dimKey].type==='number';
        var dimMap = {};
        rows.forEach(function(r){ valuesOf(r, dimKey).forEach(function(dl){ if (!dimMap[dl]) dimMap[dl]={label:dl, rows:[]}; dimMap[dl].rows.push(r); }); });
        var entries = Object.keys(dimMap).map(function(k){ return dimMap[k]; });
        entries.forEach(function(e){ e.total = aggregate(measure, mfield, e.rows); });
        if (dimNumeric){ entries.sort(function(a,b){ return parseFloat(a.label)-parseFloat(b.label); }); }
        else if (sortMode==='label'){ entries.sort(function(a,b){ return String(a.label).localeCompare(String(b.label)); }); }
        else if (sortMode==='val_asc'){ entries.sort(function(a,b){ return a.total-b.total; }); }
        else { entries.sort(function(a,b){ return b.total-a.total; }); }
        if (!dimNumeric && topN && entries.length>topN){
          var kept = entries.slice(0, topN), rest = entries.slice(topN);
          if (useOther && rest.length){ var orows=[]; rest.forEach(function(e){ orows=orows.concat(e.rows); }); kept.push({ label:'Other', rows:orows, total:aggregate(measure,mfield,orows) }); }
          entries = kept;
        }
        var labels = entries.map(function(e){ return labelFor(e.label, dimKey); });
        if (!splitKey){
          return { labels:labels, datasets:[{ label:measureLbl(measure,mfield), data:entries.map(function(e){ return round2(e.total); }) }] };
        }
        var stot = {}; rows.forEach(function(r){ valuesOf(r, splitKey).forEach(function(sv){ stot[sv]=(stot[sv]||0)+1; }); });
        var snames = Object.keys(stot).sort(function(a,b){ return stot[b]-stot[a]; }).slice(0, 8);
        var datasets = snames.map(function(sn){
          return { label:sn, data:entries.map(function(e){ var sub=e.rows.filter(function(r){ return valuesOf(r,splitKey).indexOf(sn)!==-1; }); return round2(aggregate(measure,mfield,sub)); }) };
        });
        return { labels:labels, datasets:datasets };
      }

      function applyColors(cfg, type, split){
        var ds = cfg.data.datasets;
        if (type==='pie' || type==='doughnut'){
          ds[0].backgroundColor = cfg.data.labels.map(function(_, i){ return PALETTE[i % PALETTE.length]; });
        } else if (split){
          ds.forEach(function(d, i){ var c = PALETTE[i % PALETTE.length];
            if (type==='line' || type==='area'){ d.borderColor=c; d.backgroundColor=hexA(c,0.15); d.fill=(type==='area'); d.tension=0.2; }
            else { d.backgroundColor=c; } });
        } else if (type==='line' || type==='area'){
          ds[0].borderColor='#2ecc71'; ds[0].backgroundColor='rgba(46,204,113,0.15)'; ds[0].fill=(type==='area'); ds[0].tension=0.2;
        } else {
          ds[0].backgroundColor = cfg.data.labels.map(function(_, i){ return PALETTE[i % PALETTE.length]; });
        }
      }

      function histogram(rows, field, bins){
        var vals = rows.map(function(r){ return r[field]; }).filter(function(v){ return v!=null && !isNaN(v); });
        if (!vals.length) return { labels:[], data:[] };
        var mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
        if (mn === mx) mx = mn + 1;
        var w = (mx - mn) / bins, counts = []; for (var z=0; z<bins; z++) counts.push(0);
        vals.forEach(function(v){ var b = Math.floor((v - mn) / w); if (b >= bins) b = bins-1; if (b < 0) b = 0; counts[b]++; });
        var yr = isYearCol(field);
        var labels = []; for (var i=0;i<bins;i++){
          var a = Math.round(mn + i*w), b2 = Math.round(mn + (i+1)*w);
          labels.push(yr ? fmtYearRange(a, b2) : (a + '\u2013' + b2));
        }
        return { labels:labels, data:counts };
      }

      function buildChart(){
        if (typeof Chart === 'undefined'){ var e = el('dt-chart-empty'); e.textContent = 'Chart library failed to load (needs internet).'; e.style.display = 'block'; return; }
        var rows = window.__dtFiltered || filteredRows();
        var t = el('dt-ctype').value, cfg = null;
        if (t==='bar' || t==='line' || t==='area' || t==='pie' || t==='doughnut'){
          var dimKey = el('dt-cdim').value, measure = el('dt-cmeasure').value, mfield = el('dt-cmfield').value;
          var split = (t==='bar'||t==='line'||t==='area') ? el('dt-csplit').value : '';
          var ser = buildSeries(rows, dimKey, split, measure, mfield, el('dt-csort').value, parseInt(el('dt-ctopn').value)||15, el('dt-cother').checked);
          var ctype = (t==='area') ? 'line' : t;
          var stacked = (t==='bar'||t==='area') && split && el('dt-cstacked').checked;
          var horiz = (t==='bar') && el('dt-choriz').checked;
          var opts = { responsive:true, maintainAspectRatio:false, plugins:{ legend:{ display:(t==='pie'||t==='doughnut'||!!split) } } };
          if (t==='bar' || t==='line' || t==='area'){ opts.scales = { x:{ stacked:stacked }, y:{ stacked:stacked, beginAtZero:true } }; if (horiz) opts.indexAxis = 'y'; }
          cfg = { type:ctype, data:{ labels:ser.labels, datasets:ser.datasets }, options:opts };
          applyColors(cfg, t, !!split);
        } else if (t==='hist'){
          var field = el('dt-cdim').value, bins = parseInt(el('dt-cbins').value)||20;
          if (colByKey[field].type!=='number') field = 'born_year';
          var h = histogram(rows, field, bins);
          cfg = { type:'bar', data:{ labels:h.labels, datasets:[{ label:'Count', data:h.data, backgroundColor:'#9b59b6' }] },
                  options:{ responsive:true, maintainAspectRatio:false, plugins:{ legend:{ display:false } }, scales:{ y:{ beginAtZero:true } } } };
        } else if (t==='scatter'){
          var xk = el('dt-cdim').value, yk = el('dt-cy').value, colorBy = el('dt-ccolor').value;
          if (colByKey[xk].type!=='number') xk = 'born_year';
          var datasets;
          if (colorBy){
            var groups = {};
            rows.forEach(function(r){ if (r[xk]==null||r[yk]==null||isNaN(r[xk])||isNaN(r[yk])) return; valuesOf(r, colorBy).forEach(function(g){ (groups[g]=groups[g]||[]).push({ x:r[xk], y:r[yk] }); }); });
            var gn = Object.keys(groups).sort(function(a,b){ return groups[b].length-groups[a].length; }).slice(0, 8);
            datasets = gn.map(function(g, i){ return { label:g, data:groups[g], backgroundColor:hexA(PALETTE[i % PALETTE.length], 0.6) }; });
          } else {
            var pts = rows.filter(function(r){ return r[xk]!=null && r[yk]!=null && !isNaN(r[xk]) && !isNaN(r[yk]); }).map(function(r){ return { x:r[xk], y:r[yk] }; });
            datasets = [{ label:colByKey[xk].label + ' vs ' + colByKey[yk].label, data:pts, backgroundColor:'rgba(52,152,219,0.55)' }];
          }
          cfg = { type:'scatter', data:{ datasets:datasets },
                  options:{ responsive:true, maintainAspectRatio:false, plugins:{ legend:{ display:!!colorBy } },
                            scales:{ x:{ title:{ display:true, text:colByKey[xk].label } }, y:{ title:{ display:true, text:colByKey[yk].label } } } } };
        }
        if (!cfg) return;
        cfg.options = cfg.options || {};
        cfg.options.plugins = cfg.options.plugins || {};
        cfg.options.plugins.title = { display:true, text:wrapLines(chartTitle(t), 58), font:{ size:14, weight:'bold' }, color:'#2c3e50', padding:{ top:2, bottom:2 } };
        cfg.options.plugins.subtitle = { display:true, text:wrapLines(activeFilterSummary(rows.length), 72), font:{ size:11, style:'italic' }, color:'#7f8c8d', padding:{ bottom:8 } };
        el('dt-chart-empty').style.display = 'none';
        if (chart) chart.destroy();
        chart = new Chart(el('dt-canvas').getContext('2d'), cfg);
        window.__dtChart = chart;
      }
      function downloadPng(){
        if (!chart) return;
        var url = chart.toBase64Image ? chart.toBase64Image() : el('dt-canvas').toDataURL('image/png');
        var a = document.createElement('a'); a.href = url; a.download = 'westfall_chart.png';
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
      }

      // ---------- wire up ----------
      function init(){
        renderHead();
        initCatSelect(); refreshPills();
        setupChartControls();
        renderBody();
        el('dt-search').oninput = renderBody;
        el('dt-ymin').oninput = renderBody;
        el('dt-ymax').oninput = renderBody;
        el('dt-yearfield').onchange = renderBody;
        Array.prototype.forEach.call(document.querySelectorAll('input[name="dt-mode"]'), function(r){ r.onchange = function(){ renderBody(); pushDataToMap(); }; });
        el('dt-clear').onclick = function(){ pills = []; el('dt-search').value=''; el('dt-ymin').value=''; el('dt-ymax').value=''; el('dt-yearfield').value='born_year'; refreshPills(); renderBody(); pushDataToMap(); };
        el('dt-export').onclick = exportCsv;
        el('dt-cbuild').onclick = buildChart;
        el('dt-cpng').onclick = downloadPng;
        el('dt-toggle-charts').onclick = function(){
          var p = el('dt-chartpanel'); p.classList.toggle('dt-hidden');
          el('dt-toggle-charts').textContent = p.classList.contains('dt-hidden') ? 'Show charts' : 'Hide charts';
          if (chart) chart.resize();
        };
        // Click any row to open the full-record detail view.
        el('dt-tbody').onclick = function(ev){
          var tr = ev.target.closest ? ev.target.closest('tr') : null;
          if (!tr) return; var ri = tr.getAttribute('data-ri'); if (ri == null) return;
          openDetail((window.__dtRows || [])[parseInt(ri)]);
        };
        el('dt-modal-map').onclick = showOnMap;
        el('dt-modal-close').onclick = closeDetail;
        el('dt-modal').onclick = function(ev){ if (ev.target === el('dt-modal')) closeDetail(); };
        document.addEventListener('keydown', function(ev){ if (ev.key === 'Escape') closeDetail(); });
        window.dashOnShow = window.dashOnShow || {};
        window.dashOnShow['page-data'] = function(){ if (chart) chart.resize(); };
        // Once the Map is built, wrap its filter engine so applying a query there
        // mirrors the category pills back into the Data tab.
        window.addEventListener('load', function(){
          if (window.filterMapData && !window.__filterWrapped){
            window.__filterWrapped = true;
            var orig = window.filterMapData;
            window.filterMapData = function(){ var r = orig.apply(this, arguments); try { pullFromMapToData(); } catch(e){} return r; };
          }
        });
      }
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
    })();
    </script>"""

    def _json(obj):
        return json.dumps(obj).replace('</', '<\\/')

    script = (script
              .replace('__FMT_YEAR__', FMT_YEAR_JS)
              .replace('__SHOW_DISCIPLINE_STAT__', _json('true' if SHOW_UNTESTED_FIELDS else 'false'))
              .replace('__YEAR_COLUMNS__', _json(list(YEAR_COLUMNS)))
              .replace('__UNKNOWN_TOKENS__', _json([t.lower() for t in CHART_UNKNOWN_TOKENS]))
              .replace('__RECORDS__', _json(records))
              .replace('__COLUMNS__', _json(columns))
              .replace('__FILTER_META__', _json(filter_meta))
              .replace('__CAT_OPTIONS__', _json(category_options))
              .replace('__VALUE_LABELS__', _json(value_label_maps)))

    return '<div id="page-data" class="dash-page">' + style + markup + script + '</div>'


# ============================ PRESENTATION LAYER: MAP ============================
MAP_CSS = """<style>
#page-map { overflow: hidden; }
#map { position: absolute; top: 0; left: 0; right: 0; bottom: 0; }

/* Panel stacks. Left and right both cascade DOWN from the top so the two sides mirror. */
.map-stack { position: absolute; z-index: 500; display: flex; flex-direction: column; gap: 10px;
    max-height: calc(100% - 20px); pointer-events: none; }
.map-stack-tl { top: 10px; left: 10px; width: 250px; }
.map-stack-tr { top: 10px; right: 10px; width: 232px; align-items: stretch; }
.map-stack > * { pointer-events: auto; }

.ui-card { background: #fff; padding: 12px 14px; border-radius: 4px;
    box-shadow: 0 1px 5px rgba(0,0,0,0.4); font-family: "Helvetica Neue", Arial, sans-serif;
    font-size: 12px; color: #2c3e50; box-sizing: border-box; }
.ui-card-header { display: flex; align-items: center; justify-content: space-between; cursor: pointer;
    user-select: none; -webkit-user-select: none; border-bottom: 1px solid #eee;
    padding-bottom: 4px; margin-bottom: 8px; }
.ui-card-header b { font-size: 13px; color: #2c3e50; }
.ui-chevron { transition: transform 0.15s; color: #7f8c8d; font-size: 11px; }
.ui-card-body.ui-collapsed { display: none; }

.query-pill { display: inline-flex; align-items: center; background: #e67e22; color: #fff;
    padding: 4px 8px; margin: 3px; border-radius: 4px; font-size: 11px; font-weight: bold;
    border: 1px solid #d35400; box-shadow: 0 1px 3px rgba(0,0,0,0.2); }
.query-pill .qp-x { cursor: pointer; color: #fff; margin-left: 7px; }
.query-select-node { width: 100%; padding: 4px; margin-bottom: 6px; border-radius: 3px;
    border: 1px solid #bdc3c7; font-size: 11px; color: #34495e; background: #fafafa; }
.query-btn { padding: 5px 10px; font-size: 11px; font-weight: bold; border-radius: 3px;
    cursor: pointer; border: none; margin-right: 4px; transition: background 0.2s; }
.query-btn:hover { filter: brightness(1.07); }
#query-master-dropzone { min-height: 48px; border: 2px dashed #34495e; background: #f8f9fa;
    border-radius: 4px; padding: 6px; box-sizing: border-box; margin-bottom: 10px; }
#query-drop-placeholder { color: #95a5a6; font-size: 11px; display: block; text-align: center; margin-top: 12px; }

.dual-slider-input::-webkit-slider-thumb { pointer-events: auto; position: relative; z-index: 99;
    -webkit-appearance: none; width: 14px; height: 14px; border-radius: 50%; background: #3498db;
    cursor: pointer; box-shadow: 0 1px 3px rgba(0,0,0,0.5); }
.dual-slider-input::-moz-range-thumb { pointer-events: auto; position: relative; z-index: 99;
    width: 14px; height: 14px; border-radius: 50%; background: #3498db; cursor: pointer;
    border: none; box-shadow: 0 1px 3px rgba(0,0,0,0.5); }

/* Layers panel (MapLibre has no built-in layer control; this is ours) */
.layer-row { display: flex; align-items: center; gap: 7px; padding: 3px 0; cursor: pointer; }
.layer-row input { cursor: pointer; }
.layer-swatch { width: 11px; height: 11px; border-radius: 50%; border: 1.5px solid #2c3e50; flex: 0 0 auto; }
/* Heatmaps read as a density ramp, not a solid dot, so they can't be confused with clusters. */
.layer-swatch-heat { width: 18px; height: 10px; border-radius: 2px; border: 1px solid #7a8894; flex: 0 0 auto; }
.layer-swatch-heat.heat-birth { background: linear-gradient(90deg, #c8e6c9 0%, #81c784 35%, #43a047 70%, #1b5e20 100%); }
.layer-swatch-heat.heat-edu { background: linear-gradient(90deg, #fdebd0 0%, #f5b041 35%, #e67e22 70%, #7e3f0c 100%); }
.layer-swatch-heat.heat-death { background: linear-gradient(90deg, #ffcdd2 0%, #e57373 35%, #e53935 70%, #8e0000 100%); }
/* Life vectors read as a line segment. */
.layer-swatch-line { width: 18px; height: 0; border-top: 2px solid #7f8c8d; flex: 0 0 auto; }
/* Basemap controls are a different kind of thing from the data layers above them. */
.layer-divider { margin: 7px 0 3px; padding-top: 6px; border-top: 1px solid #eee;
    font-size: 9.5px; font-weight: bold; letter-spacing: 0.5px; text-transform: uppercase; color: #95a5a6; }
.layer-swatch-text { width: 11px; height: 11px; flex: 0 0 auto; font-size: 10px; font-weight: bold;
    line-height: 11px; text-align: center; color: #5a6b7a; font-family: Georgia, serif; }
/* Borders read as the dashed line a boundary is drawn with. */
.layer-swatch-border { width: 13px; height: 11px; flex: 0 0 auto; position: relative; }
.layer-swatch-border::before { content: ''; position: absolute; top: 5px; left: 0; right: 0;
    border-top: 2px dashed #9aa7b2; }

#person-search-input { width: 100%; box-sizing: border-box; padding: 5px 22px 5px 7px;
    border: 1px solid #bdc3c7; border-radius: 3px; font-size: 12px; }
#person-search-clear { position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
    cursor: pointer; color: #95a5a6; font-weight: bold; display: none; }
#person-search-suggestions { display: none; position: absolute; left: 0; right: 0; top: 100%; z-index: 1000;
    background: #fff; border: 1px solid #bdc3c7; border-top: none; border-radius: 0 0 3px 3px;
    max-height: 240px; overflow-y: auto; box-shadow: 0 4px 8px rgba(0,0,0,0.15); }
.person-suggestion { padding: 5px 7px; cursor: pointer; font-size: 12px; border-bottom: 1px solid #f2f2f2; }
.person-suggestion:hover { background: #f5f7fa; }
/* The alias that caused a match, so an unexpected result explains itself. */
.ps-alt { display: block; font-size: 10.5px; color: #8e44ad; margin-top: 1px; }

#clear-sel-btn { display: none; background: #8e44ad; color: #fff; border: none; padding: 7px 13px;
    border-radius: 4px; font-family: "Helvetica Neue", Arial, sans-serif; font-size: 12px;
    font-weight: bold; cursor: pointer; box-shadow: 0 1px 5px rgba(0,0,0,0.4); width: 100%; }

.map-legend-card { background: rgba(255,255,255,0.95); padding: 8px 11px; border-radius: 4px;
    box-shadow: 0 1px 5px rgba(0,0,0,0.35); font-family: "Helvetica Neue", Arial, sans-serif;
    font-size: 11.5px; color: #2c3e50; line-height: 1.7; }
.map-legend-card .legend-item { position: relative; cursor: help; }
.map-legend-card .legend-tip { display: none; position: absolute; right: calc(100% + 12px); top: 50%;
    transform: translateY(-50%); width: 210px; background: #1f2d3a; color: #ecf0f1; font-size: 11px;
    line-height: 1.5; font-weight: normal; padding: 9px 11px; border-radius: 6px;
    box-shadow: 0 3px 10px rgba(0,0,0,0.35); z-index: 1200; white-space: normal; pointer-events: none; }
.map-legend-card .legend-item:hover .legend-tip { display: block; }
.map-legend-card .legend-tip::after { content: ''; position: absolute; left: 100%; top: 50%;
    transform: translateY(-50%); border: 6px solid transparent; border-left-color: #1f2d3a; }

.maplibregl-popup-content { padding: 10px 12px; border-radius: 5px; box-shadow: 0 2px 10px rgba(0,0,0,0.3); }

/* Life-vector hover tooltip: "Name: Birthplace -> Place of death" */
.vector-tip { pointer-events: none; }
.vector-tip .maplibregl-popup-content {
    background: #1f2d3a; color: #ecf0f1; padding: 6px 10px; border-radius: 5px;
    font-family: "Helvetica Neue", Arial, sans-serif; font-size: 11.5px; line-height: 1.5;
    box-shadow: 0 3px 10px rgba(0,0,0,0.35);
    /* Short chains size to their text; long ones wrap instead of running off-screen.
       break-word keeps a single very long place name from overflowing the box. */
    width: max-content; max-width: 280px;
    white-space: normal; overflow-wrap: break-word;
}
.vector-tip.maplibregl-popup { max-width: 280px !important; }
.vector-tip .vt-name { font-weight: bold; color: #fff; }
.vector-tip .vt-arrow { color: #f39c12; font-weight: bold; padding: 0 2px; white-space: nowrap; }
.vector-tip .maplibregl-popup-tip { display: none; }
.mp-popup { font-family: Arial, sans-serif; min-width: 200px; max-width: 320px; }
.mp-popup h4 { margin: 0 0 5px 0; color: #2c3e50; }
.mp-popup .mp-scroll { max-height: 280px; overflow-y: auto; padding-right: 4px; font-size: 12px; line-height: 1.45; }
.mp-popup .mp-sources { max-height: 90px; overflow-y: auto; font-size: 10.5px; color: #555;
    border-left: 2px solid #eee; padding-left: 6px; margin-top: 2px; }
.mp-popup .mp-tl-btn { margin-top: 9px; width: 100%; background: #e67e22; color: #fff; border: none;
    padding: 6px 10px; border-radius: 4px; font-family: "Helvetica Neue", Arial, sans-serif;
    font-size: 11.5px; font-weight: bold; cursor: pointer; }
.mp-popup .mp-tl-btn:hover { background: #ca6f1e; }
.mp-popup .mp-disclaimer { background: #fdf3e3; border-left: 3px solid #e67e22; color: #7e5109;
    font-size: 11px; line-height: 1.5; padding: 6px 8px; border-radius: 3px; margin-bottom: 6px; }
/* Roster: name on the left, dates in aligned columns on the right. The <ul> is the
   grid and each <li> is display:contents, so every row's cells share one set of
   columns -- that is what makes the dates line up down the popup. */
.mp-popup .mp-roster {
    list-style: none; margin: 4px 0 0; padding: 0;
    display: grid; grid-template-columns: 1fr auto auto auto;
    column-gap: 0; row-gap: 2px; font-size: 11.5px; line-height: 1.6;
    align-items: baseline;
}
.mp-popup .mp-roster li { display: contents; }
.mp-popup .mp-person { min-width: 0; overflow-wrap: break-word; padding-right: 10px; }
.mp-popup .mp-y-start, .mp-popup .mp-y-dash, .mp-popup .mp-y-end {
    color: #7f8c8d; white-space: nowrap;
    /* tabular figures: every digit the same width, so the columns stay true */
    font-variant-numeric: tabular-nums; font-feature-settings: "tnum";
}
.mp-popup .mp-y-start { text-align: right; }   /* start years right-align onto the dash */
.mp-popup .mp-y-dash { padding: 0 1px; }
.mp-popup .mp-y-end { text-align: left; }      /* end years left-align off the dash */

/* ----- Education timeline modal ----- */
.etl-backdrop { display: none; position: fixed; inset: 0; z-index: 4000;
    background: rgba(20,28,36,0.55); align-items: center; justify-content: center; padding: 24px; }
.etl-backdrop.etl-open { display: flex; }
.etl-modal { background: #fff; border-radius: 8px; box-shadow: 0 12px 40px rgba(0,0,0,0.35);
    width: min(860px, 100%); max-height: min(620px, 90vh); display: flex; flex-direction: column;
    font-family: "Helvetica Neue", Arial, sans-serif; color: #2c3e50; overflow: hidden; }
.etl-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
    padding: 16px 18px 12px; border-bottom: 1px solid #eef1f4; }
.etl-head h3 { margin: 0 0 3px; font-size: 16px; }
.etl-sub { font-size: 12px; color: #7f8c8d; }
.etl-close { border: none; background: transparent; font-size: 22px; line-height: 1; color: #95a5a6;
    cursor: pointer; padding: 0 4px; }
.etl-close:hover { color: #2c3e50; }
.etl-close:focus-visible { outline: 2px solid #8e44ad; border-radius: 3px; }
.etl-body { padding: 14px 18px 18px; overflow: auto; }

/* Rows share one grid so the tracks line up under a single axis. */
.etl-grid { display: grid; grid-template-columns: minmax(84px, 170px) 1fr auto;
    column-gap: 10px; row-gap: 3px; align-items: center; }
.etl-axis-label { font-size: 10px; color: #95a5a6; }
.etl-axis { position: relative; height: 18px; border-bottom: 1px solid #dfe4e8; }
.etl-tick { position: absolute; top: 0; bottom: 0; border-left: 1px solid #eef1f4; }
.etl-tick span { position: absolute; bottom: 1px; left: 3px; font-size: 9.5px; color: #95a5a6;
    font-variant-numeric: tabular-nums; white-space: nowrap; }
.etl-name { font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.etl-yrs { font-size: 11px; color: #7f8c8d; font-variant-numeric: tabular-nums; white-space: nowrap; }
.etl-track { position: relative; height: 16px; }
/* faint gridlines carried down through every row, so the eye can read across */
.etl-track .etl-gl { position: absolute; top: 0; bottom: 0; border-left: 1px solid #f4f6f8; }
.etl-bar { position: absolute; top: 4px; height: 8px; border-radius: 4px; background: #e67e22;
    min-width: 3px; }
/* An open end means the source records one year only: fade out rather than invent a span. */
.etl-open-r { position: absolute; top: 4px; height: 8px; width: 34px;
    background: linear-gradient(90deg, #e67e22, rgba(230,126,34,0)); border-radius: 4px 0 0 4px; }
.etl-open-l { position: absolute; top: 4px; height: 8px; width: 34px;
    background: linear-gradient(270deg, #e67e22, rgba(230,126,34,0)); border-radius: 0 4px 4px 0; }
.etl-row-hit { cursor: pointer; }
.etl-row-hit:hover .etl-bar, .etl-row-hit:hover .etl-open-r, .etl-row-hit:hover .etl-open-l { background: #8e44ad; }
.etl-row-hit:hover .etl-open-r { background: linear-gradient(90deg, #8e44ad, rgba(142,68,173,0)); }
.etl-row-hit:hover .etl-open-l { background: linear-gradient(270deg, #8e44ad, rgba(142,68,173,0)); }
.etl-row-hit:hover .etl-name { color: #8e44ad; font-weight: bold; }
.etl-undated { margin-top: 14px; padding-top: 12px; border-top: 1px solid #eef1f4;
    font-size: 11.5px; color: #7f8c8d; line-height: 1.7; }
.etl-undated b { color: #2c3e50; }
.etl-empty { color: #9aa7b2; font-style: italic; font-size: 13px; background: #fafbfc;
    border: 1px dashed #cfd8e0; border-radius: 6px; padding: 14px 16px; }
.etl-legend { margin-top: 12px; font-size: 10.5px; color: #95a5a6; display: flex; flex-wrap: wrap; gap: 14px; }
.etl-legend i { display: inline-block; vertical-align: middle; margin-right: 5px; }

/* Small screens: keep the map usable rather than buried under panels. */
@media (max-width: 720px) {
    .map-stack-tl { width: calc(100% - 20px); max-width: 250px; }
    .map-stack-tr { width: 200px; }
}

/* --- Map chrome (bottom-right): compass, scale, zoom, attribution --- */
/* Zoom buttons keep MapLibre's default 29px sizing; we only add a focus ring. */
.maplibregl-ctrl-bottom-right { z-index: 400; }
.maplibregl-ctrl-group { box-shadow: 0 1px 5px rgba(0,0,0,0.4); }
.maplibregl-ctrl-group button:hover { background-color: #eef7f1; }
.maplibregl-ctrl-group button:focus-visible { outline: 3px solid #8e44ad; outline-offset: -3px; }
.maplibregl-ctrl-scale {
    background: rgba(255,255,255,0.95); border: 2px solid #7a8894; border-top: none;
    border-radius: 0 0 3px 3px; font-family: "Helvetica Neue", Arial, sans-serif;
    font-size: 12px; line-height: 1.5; color: #2c3e50; padding: 2px 7px;
}
/* Scale bar mounted into the right-hand panel stack, directly beneath the legend. */
.scale-under-legend {
    margin: 0 0 0 auto;   /* right-align under the legend card */
    box-shadow: 0 1px 5px rgba(0,0,0,0.25);
}
.maplibregl-ctrl-attrib.maplibregl-compact { background: rgba(255,255,255,0.9); }
.maplibregl-ctrl-attrib-button:focus-visible { outline: 3px solid #8e44ad; }

</style>"""


MAP_MARKUP = """<div id="page-map" class="dash-page dash-active">
  <div id="map"></div>

  <!-- LEFT STACK: Temporal Controller on top, Advanced Query Builder beneath -->
  <div class="map-stack map-stack-tl">
    <div class="ui-card" id="card-temporal">
      <div class="ui-card-header" data-toggle><b>Temporal Controller</b><span class="ui-chevron">&#9662;</span></div>
      <div class="ui-card-body">
        <label style="display:flex; align-items:center; font-weight:bold; cursor:pointer; margin-bottom:8px;">
          <input type="checkbox" id="temporal-enable" style="margin-right:6px;"> Filter by Timeline</label>
        <div id="slider-wrapper" style="display:none; padding-top:5px;">
          <div style="position:relative; width:100%; height:15px; margin-bottom:12px;">
            <div style="position:absolute; width:100%; height:5px; background:#ddd; top:5px; border-radius:3px; z-index:1;"></div>
            <input type="range" id="slider-min" class="dual-slider-input" style="position:absolute; width:100%; top:0; left:0; pointer-events:none; -webkit-appearance:none; background:none; margin:0;">
            <input type="range" id="slider-max" class="dual-slider-input" style="position:absolute; width:100%; top:0; left:0; pointer-events:none; -webkit-appearance:none; background:none; margin:0;">
          </div>
          <div style="display:flex; justify-content:space-between; font-weight:bold; color:#34495e; font-size:11px;">
            <span>Min: <span id="lbl-min"></span></span><span>Max: <span id="lbl-max"></span></span>
          </div>
        </div>
        <label style="display:flex; align-items:center; font-weight:bold; cursor:pointer; margin-top:10px; border-top:1px solid #eee; padding-top:8px;">
          <input type="checkbox" id="playback-enable" style="margin-right:6px;"> Playback</label>
        <div id="play-controls" style="display:none; margin-top:8px;">
          <div style="text-align:center; margin-bottom:6px;">
            <div style="font-size:9.5px; color:#95a5a6; text-transform:uppercase; letter-spacing:0.5px;">Current Year</div>
            <div id="play-year-display" style="font-size:20px; font-weight:bold; color:#2c3e50; line-height:1.15;">All years</div>
          </div>
          <div style="display:flex; gap:6px;">
            <button id="play-btn" class="query-btn" style="background:#2ecc71; color:#fff; flex:1; margin-right:0;">&#9654; Play</button>
            <button id="pause-btn" class="query-btn" style="background:#e67e22; color:#fff; flex:1; margin-right:0;">&#10073;&#10073; Pause</button>
          </div>
          <div style="display:flex; align-items:center; justify-content:space-between; margin-top:8px; font-size:11px;">
            <span style="font-weight:bold;">Speed</span>
            <select id="play-speed" class="query-select-node" style="width:auto; margin-bottom:0; padding:3px 6px;">
              <option value="0.25">0.25x</option><option value="0.50">0.50x</option><option value="0.75">0.75x</option>
              <option value="1.00" selected>1.00x</option><option value="1.25">1.25x</option><option value="1.50">1.50x</option>
              <option value="1.75">1.75x</option><option value="2.00">2.00x</option>
              <option value="5.00">5.00x</option>
            </select>
          </div>
        </div>
      </div>
    </div>

    <div class="ui-card" id="card-query" style="max-height:70vh; overflow-y:auto;">
      <div class="ui-card-header" data-toggle><b>Advanced Query Builder</b><span class="ui-chevron">&#9662;</span></div>
      <div class="ui-card-body">
        <div id="query-category-selects" style="max-height:230px; overflow-y:auto; padding-right:4px; margin-bottom:8px; border-bottom:1px solid #eee;"></div>
        <div style="margin-top:8px; font-weight:bold; margin-bottom:4px;">Active Filters:</div>
        <div id="query-master-dropzone">
          <span id="query-drop-placeholder">Choose options above to add filters</span>
        </div>
        <div style="margin-bottom:8px; font-size:11px;">
          <span style="font-weight:bold;">Match:</span>
          <label style="margin-left:6px; cursor:pointer;"><input type="radio" name="query-match-mode" value="and" checked> ALL (AND)</label>
          <label style="margin-left:10px; cursor:pointer;"><input type="radio" name="query-match-mode" value="or"> ANY (OR)</label>
        </div>
        <div style="display:flex;">
          <button class="query-btn" id="btn-apply-query" style="background:#2ecc71; color:#fff; flex:2;">Apply Query</button>
          <button class="query-btn" id="btn-clear-query" style="background:#95a5a6; color:#fff; flex:1; margin-right:0;">Clear</button>
        </div>
        <button class="query-btn" id="btn-export-selection" style="background:#3498db; color:#fff; width:100%; margin-right:0; margin-top:8px;">&#11015; Export selection (CSV)</button>
      </div>
    </div>
  </div>

  <!-- RIGHT STACK: Layers, Top Hubs, Find a Person, Clear selection -->
  <div class="map-stack map-stack-tr">
    <div class="ui-card" id="card-layers">
      <div class="ui-card-header" data-toggle><b>Layers</b><span class="ui-chevron">&#9662;</span></div>
      <div class="ui-card-body" id="layers-body">
        <label class="layer-row"><input type="checkbox" id="lyr-births" checked><span class="layer-swatch" style="background:#2ecc71;"></span>Birth Clusters</label>
        <label class="layer-row"><input type="checkbox" id="lyr-education"><span class="layer-swatch" style="background:#e67e22;"></span>Education Institutions</label>
        <label class="layer-row"><input type="checkbox" id="lyr-deaths"><span class="layer-swatch" style="background:#e74c3c;"></span>Death Clusters</label>
        <label class="layer-row"><input type="checkbox" id="lyr-vectors"><span class="layer-swatch-line"></span>Life Vectors</label>
        <label class="layer-row"><input type="checkbox" id="lyr-birthheat"><span class="layer-swatch-heat heat-birth"></span>Birth Heatmap</label>
        <label class="layer-row"><input type="checkbox" id="lyr-eduheat"><span class="layer-swatch-heat heat-edu"></span>Education Heatmap</label>
        <label class="layer-row"><input type="checkbox" id="lyr-deathheat"><span class="layer-swatch-heat heat-death"></span>Death Heatmap</label>
        <div class="layer-divider">Basemap</div>
        <label class="layer-row"><input type="checkbox" id="lyr-basemap-labels" checked><span class="layer-swatch-text">A</span>Place Labels</label>
        <label class="layer-row"><input type="checkbox" id="lyr-basemap-borders" checked><span class="layer-swatch-border"></span>Borders</label>
      </div>
    </div>

    <div class="ui-card" id="card-search">
      <div class="ui-card-header" data-toggle><b>Find a Person</b><span class="ui-chevron">&#9662;</span></div>
      <div class="ui-card-body">
        <div style="position:relative;">
          <input id="person-search-input" type="text" autocomplete="off" placeholder="Type a name...">
          <span id="person-search-clear" title="Clear">&times;</span>
          <div id="person-search-suggestions"></div>
        </div>
      </div>
    </div>

    <button id="clear-sel-btn">&#10005;&nbsp; Clear selection</button>

    <div class="map-legend-card">
      <div style="font-weight:bold; margin-bottom:2px; font-size:12px;">Legend</div>
      <div class="legend-item"><span style="display:inline-block; width:11px; height:11px; border-radius:50%; background:#2ecc71; border:1.5px solid #2c3e50; vertical-align:middle; margin-right:7px;"></span>Place of Birth<span class="legend-tip">The location where the figure was born. Green markers cluster into birth hubs &mdash; click one to zoom in and see everyone born there.</span></div>
      <div class="legend-item"><span style="display:inline-block; width:11px; height:11px; border-radius:50%; background:#e67e22; border:1.5px solid #2c3e50; vertical-align:middle; margin-right:7px;"></span>Place of Education<span class="legend-tip">Where the figure studied. A person may have several, shown in the order they were attended. Click a marker for the institution and the years there.</span></div>
      <div class="legend-item"><span style="display:inline-block; width:11px; height:11px; border-radius:50%; background:#e74c3c; border:1.5px solid #2c3e50; vertical-align:middle; margin-right:7px;"></span>Place of Death<span class="legend-tip">The location where the figure died. Red markers cluster into death hubs &mdash; click one to zoom in and see everyone who died there.</span></div>
      <div class="legend-item"><span style="display:inline-block; width:16px; border-top:2px solid #7f8c8d; vertical-align:middle; margin-right:7px;"></span>Life Vector<span class="legend-tip">A line tracing a scholar's journey: birthplace, then each place of education in the order attended, then their place of death. Toggle these on or off in the Layers panel.</span></div>
    </div>
  </div>

  <!-- EDUCATION TIMELINE (opened from an education bubble's popup) -->
  <div id="edu-timeline" class="etl-backdrop" aria-hidden="true">
    <div class="etl-modal" role="dialog" aria-modal="true" aria-labelledby="etl-title">
      <div class="etl-head">
        <div class="etl-heading">
          <h3 id="etl-title">Institution</h3>
          <div class="etl-sub" id="etl-sub"></div>
        </div>
        <button type="button" id="etl-close" class="etl-close" aria-label="Close timeline">&times;</button>
      </div>
      <div class="etl-body" id="etl-body"></div>
    </div>
  </div>

</div>"""


MAP_JS_A = r"""<script>
(function(){
  var SITE = __SITE__;
  var STYLE_URL = __STYLE_URL__;
  var BASEMAP = __BASEMAP__;                 // 'ohm' | 'maptiler'
  var LABEL_FONT = __LABEL_FONT__;           // must exist on the style's glyph server
  var BASEMAP_DATE_STEP = __BASEMAP_DATE_STEP__;
  var LABEL_LANG = __LABEL_LANG__;           // '' = leave OHM's local-language labels alone
  var INFRA_PREFIXES = __INFRA_PREFIXES__;   // source-layer prefixes counted as infrastructure
  var BORDER_SLS = __BORDER_SOURCE_LAYERS__; // source-layers holding admin boundaries
  var VIEW_BOUNDS = __VIEW_BOUNDS__;         // the whole world, fitted to the viewport
  var CAT_CODE = __FILTER_CODES__;           // category -> short code, for the permalink

  var people = SITE.people, categories = SITE.categories, categoryMeta = SITE.categoryMeta;
  var noteMeta = SITE.noteMeta, valueLabelMaps = SITE.valueLabelMaps;
  var dataMinYear = SITE.minYear, dataMaxYear = SITE.maxYear;

  var labelMap = {}; categoryMeta.forEach(function(m){ labelMap[m.key] = m.label; });
  var map = null, mapReady = false, pendingFilter = false, popup = null;

  function el(id){ return document.getElementById(id); }
  function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
  function clean(v){ var s = (v==null)?'':String(v).trim(); return (['','nan','none','unknown'].indexOf(s.toLowerCase())!==-1)?'':s; }
__FMT_YEAR__
  function normNum(s){ var f = parseFloat(s); return (!isNaN(f) && Math.floor(f)===f) ? String(f) : s; }

  // ---------- popup (ported from the Python build_popup) ----------
  function place(town, country){ return [clean(town), clean(country)].filter(Boolean).join(', '); }
  function buildPopupHtml(p){
    var bornPlace = place(p.born_town, p.born_country) || 'Unknown';
    var s = '<div><b>Born:</b> ' + esc(bornPlace) + (p.born_year!=null ? ' in ' + esc(fmtYear(p.born_year)) : '') + '</div>';
    var diedPlace = place(p.died_town, p.died_country);
    if (diedPlace || p.died_year != null){
      s += '<div><b>Died:</b> ' + esc(diedPlace || 'Unknown') + (p.died_year!=null ? ' in ' + esc(fmtYear(p.died_year)) : '') + '</div>';
    }
    var deg = clean(p.education_degrees), inst = clean(p.education_institution);
    var education = (deg && inst) ? (esc(deg) + ' from ' + esc(inst)) : esc(inst || deg);

    var occ = clean(p.father_occupation);
    var wraw = clean(p.father_is_wealthy);
    var wlbl = wraw ? (valueLabelMaps.father_is_wealthy ? (valueLabelMaps.father_is_wealthy[wraw] || valueLabelMaps.father_is_wealthy[normNum(wraw)] || '') : '') : '';
    if (wlbl === 'Unknown') wlbl = '';
    var father = (occ && wlbl) ? (esc(occ) + ' (' + esc(wlbl) + ')') : (occ ? esc(occ) : (wlbl ? esc(wlbl) + ' family' : ''));

    var rows = [
      ['Also known as', (p.alt && p.alt.length) ? p.alt.map(esc).join('; ') : ''],
      ['Gender', esc(clean(p.gender))],
      ['Education', education],
      ['Father', father],
      ['Scientific Disciplines', esc(clean(p.scientific_disciplines))],
      ['Societies', esc(clean(p.scientific_societies_membership))],
      ['Religion', esc(clean(p.religion_affiliation))],
      ['Means of Support', esc(clean(p.means_of_support))],
      ['Patronage Types', esc(clean(p.patronage_types))],
      ['Instruments', esc(clean(p.instruments))]
    ];
    // Some fields are hidden until tested; drop their rows here. Empty when the flag is on.
    var HIDDEN_ROWS = __HIDDEN_POPUP_ROWS__;
    rows.forEach(function(r){
      if (r[1] && HIDDEN_ROWS.indexOf(r[0]) === -1)
        s += '<div style="margin-top:3px;"><b>' + r[0] + ':</b> ' + r[1] + '</div>';
    });
    if (clean(p.sources)){
      s += '<div style="margin-top:6px;"><b>Sources:</b><div class="mp-sources">' + esc(p.sources) + '</div></div>';
    }
    return '<div class="mp-popup"><h4>' + esc(p.name) + '</h4>' +
           '<hr style="border:0; border-top:1px solid #ccc; margin:5px 0;">' +
           '<div class="mp-scroll">' + s + '</div></div>';
  }

  // ---------- geojson builders ----------
  function fcBirths(list){
    return { type:'FeatureCollection', features: list.map(function(p){
      return { type:'Feature', id:p.idx, properties:{ idx:p.idx, name:p.name, town:p.born_town, year:p.born_year },
               geometry:{ type:'Point', coordinates:[p.blon, p.blat] } }; }) };
  }
  function fcDeaths(list){
    return { type:'FeatureCollection', features: list.filter(function(p){ return p.dlat!=null; }).map(function(p){
      return { type:'Feature', id:p.idx, properties:{ idx:p.idx, name:p.name, town:p.died_town, year:p.died_year },
               geometry:{ type:'Point', coordinates:[p.dlon, p.dlat] } }; }) };
  }
  // The life vector now routes birth -> each education stop, in listed order -> death.
  // A person with education but no recorded death still yields a path, and someone with
  // neither education nor death coords yields nothing (a single point is not a line).
  function lifePath(p){
    var pts = [[p.blon, p.blat]];
    (p.edu || []).forEach(function(st){ pts.push([st.lon, st.lat]); });
    if (p.dlat != null) pts.push([p.dlon, p.dlat]);
    return pts;
  }
  function fcVectors(list){
    var feats = [];
    list.forEach(function(p){
      var pts = lifePath(p);
      if (pts.length < 2) return;
      feats.push({ type:'Feature', properties:{ idx:p.idx, name:p.name },
                   geometry:{ type:'LineString', coordinates: pts } });
    });
    return { type:'FeatureCollection', features: feats };
  }
  // RAW education stops: one point per person per stop. This feeds the HEATMAP, whose
  // density must reflect how many people studied somewhere -- so it is deliberately
  // NOT aggregated.
  function fcEduStops(list){
    var feats = [];
    list.forEach(function(p){
      (p.edu || []).forEach(function(st, i){
        // Raw geocode, not the fanned dot position: the heatmap shows true density.
        var lon = (st.rlon != null) ? st.rlon : st.lon;
        var lat = (st.rlat != null) ? st.rlat : st.lat;
        feats.push({ type:'Feature', properties:{ idx:p.idx, stop:i },
                     geometry:{ type:'Point', coordinates:[lon, lat] } });
      });
    });
    return { type:'FeatureCollection', features: feats };
  }

  // AGGREGATED institutions: one dot per location, labelled with how many of the
  // currently-selected people studied there. This replaces marker clustering, which
  // merged distinct institutions together and split one institution across zooms.
  // eduDetails holds the per-location roster; the GeoJSON only carries a lookup key,
  // because MapLibre flattens nested feature properties.
  var eduDetails = {};

  function fcEduInstitutions(list){
    eduDetails = {};
    list.forEach(function(p){
      (p.edu || []).forEach(function(st){
        // Grouped by INSTITUTION (st.key), never by coordinate: one geocode can cover
        // many distinct schools. The builder already gave each institution its own
        // fanned-out position, so distinct schools at one geocode stay separate dots.
        var k = st.key;
        var d = eduDetails[k];
        if (!d){
          // 'unspec:' keys pool everyone who studied in a city whose institution the
          // source never named. They sit alongside that city's named institutions as
          // one extra bubble, and carry a disclaimer in their popup.
          d = eduDetails[k] = { lat:st.lat, lon:st.lon, names:[], town:'', country:'',
                                people:[], unspec:(k.indexOf('unspec:') === 0) };
        }
        var nm = clean(st.inst);
        // "Not Specified" is a sentinel, not a name: never list it as a spelling variant.
        if (nm && nm.toLowerCase() !== 'not specified' && d.names.indexOf(nm) === -1) d.names.push(nm);
        if (!d.town) d.town = clean(st.town);
        if (!d.country) d.country = clean(st.country);
        d.people.push({ name:p.name, start:clean(st.start), end:clean(st.end), idx:p.idx });
      });
    });
    var feats = Object.keys(eduDetails).map(function(k){
      var d = eduDetails[k];
      // A person listed twice at one place (two spells of study) counts once.
      var uniq = {};
      d.people.forEach(function(x){ uniq[x.idx] = 1; });
      d.count = Object.keys(uniq).length;
      return { type:'Feature', properties:{ key:k, count:d.count,
               label:(d.unspec ? 'Not Specified' : (d.names[0] || d.town || 'Place of education')) },
               geometry:{ type:'Point', coordinates:[d.lon, d.lat] } };
    });
    return { type:'FeatureCollection', features: feats };
  }
  function fcEmpty(){ return { type:'FeatureCollection', features: [] }; }

  // Popup for an institution dot: the place, then everyone who studied there under
  // the CURRENT filters, with their years where recorded.
  // Roster years always render as (START-END). An unrecorded year shows as ???? rather
  // than collapsing the label, so every entry has the same shape and a missing year is
  // visibly missing instead of silently absent. clean() has already mapped the source's
  // literal "Unknown" to an empty string by this point.
  // Roster years are laid out as grid CELLS rather than one string, so start years,
  // the dash, and end years each get their own column. Aligning on the dash (instead
  // of just right-aligning the label) keeps the columns true even for the handful of
  // 3-digit years in the data, and lets the eye run straight down the start column to
  // see who overlapped.
  function yearCells(st, en){
    return '<span class="mp-y-start">(' + esc(st ? fmtYear(st) : '????') + '</span>' +
           '<span class="mp-y-dash">\u2013</span>' +
           '<span class="mp-y-end">' + esc(en ? fmtYear(en) : '????') + ')</span>';
  }
  function buildEduPopupHtml(key){
    var d = eduDetails[key];
    if (!d) return '<div class="mp-popup">Education location</div>';
    var title = d.unspec ? 'Not Specified' : (d.names[0] || d.town || 'Place of education');
    var where = [d.town, d.country].filter(Boolean).join(', ');

    var s = '';
    if (d.unspec){
      // These people are not grouped by school -- only by city. Say so plainly, so the
      // bubble is not misread as an institution named "Not Specified".
      s += '<div class="mp-disclaimer">These figures are recorded as having studied in ' +
           esc(d.town || 'this city') + ', but the source does not name the institution.</div>';
    } else if (d.names.length > 1){   // spelling variants of the same institution
      s += '<div><b>Also recorded as:</b> ' + d.names.slice(1).map(esc).join('; ') + '</div>';
    }
    if (where) s += '<div><b>Location:</b> ' + esc(where) + '</div>';
    s += '<div><b>Studied here:</b> ' + d.count + (d.count === 1 ? ' person' : ' people') + '</div>';

    // Sort by first recorded year, then name; undated people fall to the bottom.
    var roster = d.people.slice().sort(function(a, b){
      var ay = parseInt(a.start, 10), by2 = parseInt(b.start, 10);
      var an = isNaN(ay), bn = isNaN(by2);
      if (an && bn) return a.name.localeCompare(b.name);
      if (an) return 1;
      if (bn) return -1;
      if (ay !== by2) return ay - by2;
      return a.name.localeCompare(b.name);
    });
    s += '<div style="margin-top:7px;"><b>People</b></div><ul class="mp-roster">';
    roster.forEach(function(x){
      s += '<li><span class="mp-person">' + esc(x.name) + '</span>' +
           yearCells(x.start, x.end) + '</li>';
    });
    s += '</ul>';

    // Only offer the timeline when at least one person here is dated -- otherwise the
    // button would open an empty axis.
    var datedHere = datedPeopleCount(d);
    if (datedHere){
      // data-key rather than an inline onclick: institution keys contain apostrophes
      // ("name:st. john's college, cambridge") which would break a JS string literal.
      s += '<button type="button" class="mp-tl-btn" data-key="' + esc(key) + '">' +
           '&#128337;&nbsp; View timeline (' + datedHere + ' dated)</button>';
    }

    return '<div class="mp-popup"><h4>' + esc(title) + '</h4>' +
           '<hr style="border:0; border-top:1px solid #ccc; margin:5px 0;">' +
           '<div class="mp-scroll"><div style="color:#b9770e; font-weight:bold; margin-bottom:4px;">Education</div>' +
           s + '</div></div>';
  }

  // ---------- education timeline ----------
  // A Gantt-style view of one institution: a row per person, a bar spanning the years
  // they studied there. Only 40% of stops carry both years, so the rendering has to be
  // honest about what is missing rather than inventing spans:
  //   both years  -> a solid bar
  //   one year    -> a tick at the known year, fading out toward the unknown side
  //   no years    -> not plotted at all; listed separately beneath the axis
  function yrNum(v){ var n = parseInt(v, 10); return isNaN(n) ? null : n; }

  // Count PEOPLE, not rows. One person can have two recorded spells at an institution
  // (Aquinas was at Paris 1245-48 and again 1252-59), which would otherwise report
  // "14 people, 15 with recorded years".
  function datedPeopleCount(d){
    var seen = {};
    d.people.forEach(function(x){
      if (yrNum(x.start) !== null || yrNum(x.end) !== null) seen[x.idx] = 1;
    });
    return Object.keys(seen).length;
  }

  function niceStep(span){
    var candidates = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];
    for (var i = 0; i < candidates.length; i++){
      if (span / candidates[i] <= 8) return candidates[i];
    }
    return 1000;
  }

  function eduTimelineHtml(d){
    var rows = d.people.map(function(x){
      return { name:x.name, idx:x.idx, s:yrNum(x.start), e:yrNum(x.end) };
    });
    var dated = rows.filter(function(r){ return r.s !== null || r.e !== null; });
    // Someone with one dated stay and one undated stay is NOT an undated person: they
    // already appear on the axis, so only list people with no dates anywhere.
    var hasDate = {};
    dated.forEach(function(r){ hasDate[r.idx] = 1; });
    var seenUndated = {};
    var undated = rows.filter(function(r){
      if (r.s !== null || r.e !== null || hasDate[r.idx] || seenUndated[r.idx]) return false;
      seenUndated[r.idx] = 1;
      return true;
    });

    if (!dated.length){
      return '<div class="etl-empty">No years are recorded for anyone at this location.</div>';
    }

    // Sort by first known year so overlapping lives sit next to each other.
    dated.sort(function(a, b){
      var av = (a.s !== null ? a.s : a.e), bv = (b.s !== null ? b.s : b.e);
      if (av !== bv) return av - bv;
      return a.name.localeCompare(b.name);
    });

    var years = [];
    dated.forEach(function(r){ if (r.s !== null) years.push(r.s); if (r.e !== null) years.push(r.e); });
    var lo = Math.min.apply(null, years), hi = Math.max.apply(null, years);
    if (lo === hi){ lo -= 5; hi += 5; }                 // a single year still needs an axis
    var pad = Math.max(1, Math.round((hi - lo) * 0.06));
    lo -= pad; hi += pad;
    var span = hi - lo;
    var pos = function(y){ return ((y - lo) / span) * 100; };

    // axis ticks on round years
    var step = niceStep(span);
    var first = Math.ceil(lo / step) * step;
    var ticks = [];
    for (var y = first; y <= hi; y += step) ticks.push(y);

    var axis = '<div class="etl-axis">';
    ticks.forEach(function(y){
      axis += '<div class="etl-tick" style="left:' + pos(y).toFixed(2) + '%"><span>' + esc(fmtYear(y)) + '</span></div>';
    });
    axis += '</div>';

    var gridlines = '';
    ticks.forEach(function(y){
      gridlines += '<div class="etl-gl" style="left:' + pos(y).toFixed(2) + '%"></div>';
    });

    var html = '<div class="etl-grid">' +
               '<div class="etl-axis-label">Person</div>' + axis + '<div class="etl-axis-label"></div>';

    dated.forEach(function(r){
      var bar;
      if (r.s !== null && r.e !== null){
        var l = pos(r.s), w = pos(r.e) - pos(r.s);
        bar = '<div class="etl-bar" style="left:' + l.toFixed(2) + '%;width:' + w.toFixed(2) + '%"></div>';
      } else if (r.s !== null){
        // start known, end unknown: fade to the right
        bar = '<div class="etl-open-r" style="left:' + pos(r.s).toFixed(2) + '%"></div>' +
              '<div class="etl-bar" style="left:' + pos(r.s).toFixed(2) + '%;width:0"></div>';
      } else {
        // end known, start unknown: fade in from the left
        bar = '<div class="etl-open-l" style="left:calc(' + pos(r.e).toFixed(2) + '% - 34px)"></div>' +
              '<div class="etl-bar" style="left:' + pos(r.e).toFixed(2) + '%;width:0"></div>';
      }
      var label = (r.s !== null ? fmtYear(r.s) : '????') + '\u2013' + (r.e !== null ? fmtYear(r.e) : '????');
      html += '<div class="etl-name etl-row-hit" data-idx="' + r.idx + '" title="' + esc(r.name) + '">' + esc(r.name) + '</div>' +
              '<div class="etl-track etl-row-hit" data-idx="' + r.idx + '">' + gridlines + bar + '</div>' +
              '<div class="etl-yrs etl-row-hit" data-idx="' + r.idx + '">' + label + '</div>';
    });
    html += '</div>';

    html += '<div class="etl-legend">' +
            '<span><i style="width:22px;height:8px;border-radius:4px;background:#e67e22;"></i>years recorded</span>' +
            '<span><i style="width:22px;height:8px;border-radius:4px;background:linear-gradient(90deg,#e67e22,rgba(230,126,34,0));"></i>one year only \u2014 the other is unrecorded</span>' +
            '<span>Click a row to find that person on the map.</span></div>';

    if (undated.length){
      var names = undated.map(function(r){ return esc(r.name); }).sort().join(', ');
      html += '<div class="etl-undated"><b>' + undated.length +
              (undated.length === 1 ? ' person has' : ' people have') +
              ' no years recorded:</b> ' + names + '</div>';
    }
    return html;
  }

  window.openEduTimeline = function(key){
    var d = eduDetails[key];
    if (!d) return;
    var title = d.unspec ? 'Not Specified' : (d.names[0] || d.town || 'Place of education');
    var where = [d.town, d.country].filter(Boolean).join(', ');
    var dated = datedPeopleCount(d);
    var periods = d.people.length;

    el('etl-title').textContent = title;
    var sub = (where ? where + ' \u00b7 ' : '') +
      d.count + (d.count === 1 ? ' person' : ' people') + ' \u00b7 ' + dated + ' with recorded years';
    // Flag repeat stays, so a row count higher than the head count is explained.
    if (periods > d.count) sub += ' \u00b7 ' + periods + ' recorded stays';
    el('etl-sub').textContent = sub;
    el('etl-body').innerHTML = eduTimelineHtml(d);

    var modal = el('edu-timeline');
    modal.classList.add('etl-open');
    modal.setAttribute('aria-hidden', 'false');

    // Clicking a row highlights that person on the map, which is the point of the view:
    // spot an overlap, then go look at who it was.
    Array.prototype.forEach.call(el('etl-body').querySelectorAll('.etl-row-hit'), function(n){
      n.onclick = function(){
        var i = parseInt(n.getAttribute('data-idx'), 10);
        window.closeEduTimeline();
        if (!isNaN(i)) window.selectPerson(i);
      };
    });
  };

  window.closeEduTimeline = function(){
    var modal = el('edu-timeline');
    if (!modal) return;
    modal.classList.remove('etl-open');
    modal.setAttribute('aria-hidden', 'true');
  };

  // ---------- filter engine ----------
  function currentPills(){
    var dz = el('query-master-dropzone'); var out = [];
    if (dz) Array.prototype.forEach.call(dz.querySelectorAll('.query-pill'), function(pill){
      out.push({ category: pill.getAttribute('data-category'), value: (pill.getAttribute('data-value')||'').toLowerCase() });
    });
    return out;
  }
  function matchMode(){ var m = document.querySelector('input[name="query-match-mode"]:checked'); return m ? m.value : 'and'; }

  function personHasFilter(p, f){
    var tokens = String(p[f.category] || '').toLowerCase().split(/[,;]/).map(function(s){ return s.trim(); });
    return tokens.indexOf(f.value) !== -1;
  }

  function computeSelection(){
    var temporal = el('temporal-enable').checked;
    var mn = parseInt(el('slider-min').value), mx = parseInt(el('slider-max').value);
    var filters = currentPills(), mode = matchMode();
    return people.filter(function(p){
      if (temporal){
        var by = p.born_year, dy = p.died_year, alive;
        if (by !== null && dy !== null) alive = (by <= mx) && (dy >= mn);
        else if (by !== null) alive = (by >= mn && by <= mx);
        else if (dy !== null) alive = (dy >= mn && dy <= mx);
        else alive = false;
        if (!alive) return false;
      }
      if (filters.length === 0) return true;
      if (mode === 'or') return filters.some(function(f){ return personHasFilter(p, f); });
      return filters.every(function(f){ return personHasFilter(p, f); });
    });
  }

  // ---------- basemap date (OpenHistoricalMap) ----------
  // OHM features carry start/end dates. Unfiltered, the style draws every version of
  // every feature at once -- a city and its later renamed self stacked on top of each
  // other. So the basemap must always be pinned to a single date.
  //
  // filterByDate takes ONE date, but the Temporal Controller can express a RANGE.
  // The map therefore shows the world as of the END of the selected window: with
  // 1500-1600 selected you see the 1600 map, and during playback (where min == max)
  // the year is unambiguous. With the timeline off, it shows the latest year in the
  // catalog rather than every era at once.
  var lastBasemapYear = null;

  function basemapYear(){
    var cbx = el('temporal-enable');
    if (!cbx || !cbx.checked) return dataMaxYear;
    var mx = parseInt(el('slider-max').value);
    return isNaN(mx) ? dataMaxYear : mx;
  }

  // Re-filtering rewrites the filter on every basemap layer with a source-layer -- ~246
  // of them in OHM's style. Playback ticks every 40ms at 5x, so filtering on every tick
  // would mean thousands of rewrites per second. Only re-filter once the year has moved
  // BASEMAP_DATE_STEP years; `force` bypasses that for the moments that must be exact
  // (first paint, playback stopping, the user letting go of the slider).
  function applyBasemapDate(force){
    if (BASEMAP !== 'ohm') return;
    if (!mapReady || !map || typeof map.filterByDate !== 'function') return;
    var y = basemapYear();
    if (!force && BASEMAP_DATE_STEP > 0 && lastBasemapYear !== null &&
        Math.abs(y - lastBasemapYear) < BASEMAP_DATE_STEP) return;
    lastBasemapYear = y;
    try {
      map.filterByDate(String(y));
    } catch (e) {
      // A basemap that fails to filter is still a usable map; never take the app down.
      console.warn('basemap date filter failed for year ' + y, e);
    }
  }
  window.__applyBasemapDate = applyBasemapDate;   // exercised by the tests

  // ---------- basemap label language ----------
  // OHM's stylesheet labels everything with ['get','name'] -- the LOCAL name, so the
  // map reads "Wien", "Muenchen", "Praha". Its tiles also carry localized names under
  // name_<lang>, so swapping the expression for a coalesce gives English wherever OHM
  // has it and falls back to the local name where it does not (which, for a historical
  // map, is often the only name that exists).
  //
  // Rewritten per layer with setLayoutProperty rather than by re-applying the whole
  // style: setStyle() would diff and re-apply all 250 basemap layers AND our own
  // clustered sources, which risks re-parsing tiles for no reason.
  var NAME_EXPR = ['get', 'name'];

  function withLocalizedName(expr){
    if (!Array.isArray(expr)) return expr;
    // ['get','name'] -> ['coalesce', ['get','name_en'], ['get','name']]
    if (expr.length === 2 && expr[0] === 'get' && expr[1] === 'name'){
      return ['coalesce', ['get', 'name_' + LABEL_LANG], ['get', 'name']];
    }
    // never walk INTO a literal: its contents are data, not an expression
    if (expr[0] === 'literal') return expr;
    return expr.map(withLocalizedName);
  }

  function localizeBasemapLabels(){
    if (BASEMAP !== 'ohm' || !LABEL_LANG || !mapReady || !map) return 0;
    var changed = 0;
    try {
      map.getStyle().layers.forEach(function(layer){
        // Basemap layers only. Ours are geojson-backed and have no source-layer, and
        // their labels are cluster counts -- there is nothing to translate.
        if (!('source-layer' in layer)) return;
        var tf = layer.layout && layer.layout['text-field'];
        if (!tf) return;
        var next = withLocalizedName(tf);
        if (JSON.stringify(next) === JSON.stringify(tf)) return;
        map.setLayoutProperty(layer.id, 'text-field', next);
        changed++;
      });
    } catch (e){
      // English labels are a nicety; a failure here must not cost us the map.
      console.warn('basemap label localization failed', e);
    }
    return changed;
  }
  window.__localizeBasemapLabels = localizeBasemapLabels;   // exercised by the tests

  // ---------- strip infrastructure from the basemap ----------
  // Roads, railways, airports and buildings are removed outright rather than hidden.
  // Hiding would leave them in the style, where the date filter still rewrites them on
  // every year change -- 145 of OHM's 248 filterable layers -- for something no user
  // can ever see. Removing them makes the basemap quieter AND the timeline cheaper.
  function stripInfrastructure(){
    if (BASEMAP !== 'ohm' || !INFRA_PREFIXES.length || !mapReady || !map) return 0;
    var removed = 0;
    try {
      // getStyle() returns a snapshot, so removing while iterating it is safe
      map.getStyle().layers.forEach(function(layer){
        if (!('source-layer' in layer)) return;      // never our own geojson layers
        var sl = layer['source-layer'] || '';
        if (!INFRA_PREFIXES.some(function(p){ return sl.indexOf(p) === 0; })) return;
        if (map.getLayer(layer.id)){ map.removeLayer(layer.id); removed++; }
      });
    } catch (e){ console.warn('could not strip infrastructure layers', e); }
    return removed;
  }
  window.__stripInfrastructure = stripInfrastructure;   // exercised by the tests

  // ---------- basemap layer groups (place labels / borders) ----------
  // Two switchable groups over the basemap, each selected by what a layer IS rather
  // than by a list of ids -- OHM ships 51 label layers and 8 border layers, and no
  // hand-kept list would survive a style update.
  //
  //   labels  -> anything with a text-field (countries, cities, water)
  //   borders -> the admin hierarchy on land_ohm_lines: country, state, county, city
  //
  // They are independent today -- no border layer carries text, and the country and
  // state NAMES sit on a different source-layer, so they belong to labels. Visibility
  // is still composed from EVERY group that claims a layer, so if a future style
  // update does put text on a border layer, the two switches degrade sensibly instead
  // of one silently overruling the other.
  //
  // Each layer's ORIGINAL visibility is remembered rather than assumed to be 'visible':
  // a style is free to ship a layer hidden, and switching a group back on must not
  // reveal something the style meant to keep off.
  //
  // Our own layers are geojson-backed and carry no source-layer, which is what keeps
  // the cluster counts out of all of this.
  var GROUP_CB = { labels: 'lyr-basemap-labels', borders: 'lyr-basemap-borders' };
  var basemapGroups = null;   // id -> { shipped, groups: [...] }

  function captureBasemapGroups(){
    if (!mapReady || !map) return;
    basemapGroups = {};
    try {
      map.getStyle().layers.forEach(function(layer){
        if (!('source-layer' in layer)) return;
        var groups = [];
        if (layer.layout && layer.layout['text-field']) groups.push('labels');
        if (BORDER_SLS.indexOf(layer['source-layer']) !== -1) groups.push('borders');
        if (!groups.length) return;            // no switch claims this layer
        basemapGroups[layer.id] = {
          shipped: (layer.layout && layer.layout.visibility) || 'visible',
          groups: groups
        };
      });
    } catch (e){ console.warn('could not index basemap layer groups', e); }
  }

  function applyBasemapGroupVis(){
    if (!mapReady || !map || !basemapGroups) return 0;
    var on = {};
    Object.keys(GROUP_CB).forEach(function(g){
      var c = el(GROUP_CB[g]);
      on[g] = !c || c.checked;
    });
    var n = 0;
    Object.keys(basemapGroups).forEach(function(id){
      var info = basemapGroups[id];
      if (!map.getLayer(id)) return;
      var show = info.groups.every(function(g){ return on[g]; });
      map.setLayoutProperty(id, 'visibility', show ? info.shipped : 'none');
      n++;
    });
    return n;
  }
  window.__applyBasemapGroupVis = applyBasemapGroupVis;   // exercised by the tests
  window.__basemapGroupIndex = function(){ return basemapGroups || {}; };


  // Defined synchronously so the Data tab's window-load wrapper always finds it,
  // even though MapLibre's style loads asynchronously. Queues until the map is ready.
  window.filterMapData = function(){
    var sel = computeSelection();
    window.__currentSelection = sel;
    if (!mapReady){ pendingFilter = true; return; }
    map.getSource('births').setData(fcBirths(sel));
    map.getSource('deaths').setData(fcDeaths(sel));
    map.getSource('vectors').setData(fcVectors(sel));
    map.getSource('birthheat').setData(fcBirths(sel));
    map.getSource('deathheat').setData(fcDeaths(sel));
    // Dots are aggregated per institution; the heatmap keeps one point per person-stop
    // so its density is unchanged by the aggregation.
    map.getSource('education').setData(fcEduInstitutions(sel));
    map.getSource('eduheat').setData(fcEduStops(sel));
    // A hovered person may have just been filtered out; drop any stale hover overlay.
    if (map.getSource('hover')){ hoveredIdx = null; map.getSource('hover').setData(fcEmpty()); }
    if (window.__clearHover) window.__clearHover();
    // Every temporal change funnels through here -- slider, playback tick, toggle --
    // so this is the one place the basemap date needs syncing.
    applyBasemapDate(false);
    if (window.__writeUrl) window.__writeUrl();
  };
"""


MAP_JS_B = r"""
  // ---------- map init: MapLibre is the ONLY renderer ----------
  map = new maplibregl.Map({
    container: 'map',
    style: STYLE_URL,
    // Superseded by the fitBounds below as soon as the map loads; these only cover the
    // moment before that, and the case where the fit fails.
    center: __FALLBACK_CENTER__,
    zoom: __FALLBACK_ZOOM__,
    // 0, not 1: fitting the whole world needs zoom ~0.7 on a tablet and below 0 on a
    // phone. A floor of 1 would clamp every small screen back to a regional view.
    minZoom: 0,
    maxZoom: 18,
    // Draw a single world rather than tiling it endlessly east-west. Unlike maxBounds,
    // this does not clamp the camera: every zoom level stays available and the far
    // edges (lon -180 / +180) remain reachable. It only stops the repeat.
    renderWorldCopies: false,
    // MapLibre adds its own AttributionControl unless this is false. We add a single
    // compact one below instead -- otherwise the map shows two attribution widgets.
    attributionControl: false
  });
  // Bottom-right corner. MapLibre stacks a corner's controls in add-order, so
  // first-added renders topmost: the attribution ("i") toggle above the zoom buttons.
  //
  // Attribution stays compact -- MapTiler's terms and OpenStreetMap's ODbL licence
  // both require it to remain visible, so it is minimised, not removed.
  // OHM's stylesheet carries no attribution on any of its sources, but the data is
  // ODbL -- so the credit is added here by hand. Never remove it: it is a licence term,
  // not decoration.
  map.addControl(new maplibregl.AttributionControl({
    compact: true,
    customAttribution: __ATTRIBUTION__
  }), 'bottom-right');
  map.addControl(new maplibregl.NavigationControl({ showCompass: false, showZoom: true }), 'bottom-right');

  // The scale bar belongs directly under the legend, which lives in our own top-right
  // panel stack rather than a MapLibre corner. A control's onAdd(map) returns its DOM
  // element, so we mount it into the stack ourselves instead of calling addControl.
  (function mountScaleUnderLegend(){
    try {
      var scale = new maplibregl.ScaleControl({ maxWidth: 130, unit: 'metric' });
      var scaleEl = scale.onAdd(map);
      scaleEl.classList.add('scale-under-legend');
      var stack = document.querySelector('.map-stack-tr');
      if (stack) stack.appendChild(scaleEl);   // legend is the last child, so this lands beneath it
    } catch (e) { console.warn('scale control could not be mounted', e); }
  })();

  var CLUSTER_OPTS = { cluster: true, clusterMaxZoom: 10, clusterRadius: 50 };

  map.on('load', function(){
    // Heatmap sources reuse the same point data but render as native heatmap layers.
    map.addSource('birthheat', { type:'geojson', data: fcEmpty() });
    map.addSource('deathheat', { type:'geojson', data: fcEmpty() });
    map.addSource('vectors',   { type:'geojson', data: fcEmpty() });
    map.addSource('births', Object.assign({ type:'geojson', data: fcEmpty() }, CLUSTER_OPTS));
    map.addSource('deaths', Object.assign({ type:'geojson', data: fcEmpty() }, CLUSTER_OPTS));
    // The features fed to this source are already one-per-INSTITUTION (see
    // fcEduInstitutions), each carrying a `count` of how many selected people studied
    // there. Clustering those aggregated features gives the best of both:
    //   * zoomed out  -> nearby institutions merge into a cluster, like births/deaths
    //   * zoomed in   -> the cluster dissolves into institutions, and a university
    //                    stays ONE dot no matter how many people attended it.
    // clusterProperties sums the per-institution counts so a cluster reports PEOPLE,
    // matching what the birth and death clusters count.
    map.addSource('education', Object.assign({ type:'geojson', data: fcEmpty(),
      clusterProperties: { people: ['+', ['get', 'count']] } }, CLUSTER_OPTS));
    map.addSource('eduheat',   { type:'geojson', data: fcEmpty() });
    map.addSource('hover',     { type:'geojson', data: fcEmpty() });
    map.addSource('highlight', { type:'geojson', data: fcEmpty() });

    // --- decorative world border (bottom of our stack, above the basemap) ---
    // --- heatmaps (bottom of our stack) ---
    map.addLayer({ id:'birthheat-layer', type:'heatmap', source:'birthheat', maxzoom:13,
      layout:{ visibility:'none' },
      paint:{ 'heatmap-weight':0.4, 'heatmap-intensity':1,
        'heatmap-color':['interpolate',['linear'],['heatmap-density'],0,'rgba(0,0,0,0)',0.2,'#c8e6c9',0.4,'#81c784',0.6,'#43a047',0.8,'#2e7d32',1,'#1b5e20'],
        'heatmap-radius':22, 'heatmap-opacity':0.85 } });
    map.addLayer({ id:'deathheat-layer', type:'heatmap', source:'deathheat', maxzoom:13,
      layout:{ visibility:'none' },
      paint:{ 'heatmap-weight':0.4, 'heatmap-intensity':1,
        'heatmap-color':['interpolate',['linear'],['heatmap-density'],0,'rgba(0,0,0,0)',0.2,'#ffcdd2',0.4,'#e57373',0.6,'#e53935',0.8,'#c62828',1,'#8e0000'],
        'heatmap-radius':22, 'heatmap-opacity':0.85 } });

    map.addLayer({ id:'eduheat-layer', type:'heatmap', source:'eduheat', maxzoom:13,
      layout:{ visibility:'none' },
      paint:{ 'heatmap-weight':0.4, 'heatmap-intensity':1,
        'heatmap-color':['interpolate',['linear'],['heatmap-density'],0,'rgba(0,0,0,0)',0.2,'#fdebd0',0.4,'#f5b041',0.6,'#e67e22',0.8,'#ca6f1e',1,'#7e3f0c'],
        'heatmap-radius':22, 'heatmap-opacity':0.85 } });

    // --- life vectors ---
    map.addLayer({ id:'vector-lines', type:'line', source:'vectors',
      layout:{ visibility:'none', 'line-cap':'round' },
      paint:{ 'line-color':'#7f8c8d', 'line-width':1.5, 'line-opacity':0.5 } });

    // --- birth clusters + points ---
    map.addLayer({ id:'birth-clusters', type:'circle', source:'births', filter:['has','point_count'],
      paint:{ 'circle-color':['step',['get','point_count'],'rgba(161,238,189,0.85)',10,'rgba(46,204,113,0.9)',100,'rgba(21,103,50,0.95)'],
        'circle-radius':['step',['get','point_count'],17,10,21,100,25],
        'circle-stroke-width':2,
        'circle-stroke-color':['step',['get','point_count'],'#2ecc71',10,'#27ae60',100,'#114b24'] } });
    map.addLayer({ id:'birth-cluster-count', type:'symbol', source:'births', filter:['has','point_count'],
      layout:{ 'text-field':['get','point_count_abbreviated'], 'text-font':LABEL_FONT, 'text-size':13 },
      paint:{ 'text-color':['step',['get','point_count'],'#1e5631',10,'#ffffff'] } });
    map.addLayer({ id:'birth-points', type:'circle', source:'births', filter:['!',['has','point_count']],
      paint:{ 'circle-color':'#2ecc71', 'circle-radius':5, 'circle-opacity':0.85,
        'circle-stroke-width':1.5, 'circle-stroke-color':'#2c3e50' } });

    // --- death clusters + points (hidden by default, like the old build) ---
    map.addLayer({ id:'death-clusters', type:'circle', source:'deaths', filter:['has','point_count'],
      layout:{ visibility:'none' },
      paint:{ 'circle-color':['step',['get','point_count'],'rgba(254,200,200,0.85)',10,'rgba(231,76,60,0.9)',100,'rgba(120,20,20,0.95)'],
        'circle-radius':['step',['get','point_count'],17,10,21,100,25],
        'circle-stroke-width':2,
        'circle-stroke-color':['step',['get','point_count'],'#e74c3c',10,'#c0392b',100,'#5c1010'] } });
    map.addLayer({ id:'death-cluster-count', type:'symbol', source:'deaths', filter:['has','point_count'],
      layout:{ visibility:'none', 'text-field':['get','point_count_abbreviated'], 'text-font':LABEL_FONT, 'text-size':13 },
      paint:{ 'text-color':['step',['get','point_count'],'#78281f',10,'#ffffff'] } });
    map.addLayer({ id:'death-points', type:'circle', source:'deaths', filter:['!',['has','point_count']],
      layout:{ visibility:'none' },
      paint:{ 'circle-color':'#e74c3c', 'circle-radius':5, 'circle-opacity':0.85,
        'circle-stroke-width':1.5, 'circle-stroke-color':'#2c3e50' } });

    // --- education (orange, hidden by default) ---
    // Zoomed out: clusters of nearby institutions, labelled with total PEOPLE.
    map.addLayer({ id:'edu-clusters', type:'circle', source:'education', filter:['has','point_count'],
      layout:{ visibility:'none' },
      paint:{ 'circle-color':['step',['get','people'],'rgba(250,215,160,0.85)',10,'rgba(230,126,34,0.9)',100,'rgba(126,63,12,0.95)'],
        'circle-radius':['step',['get','people'],17,10,21,100,25],
        'circle-stroke-width':2,
        'circle-stroke-color':['step',['get','people'],'#e67e22',10,'#ca6f1e',100,'#6e3b10'] } });
    map.addLayer({ id:'edu-cluster-count', type:'symbol', source:'education', filter:['has','point_count'],
      layout:{ visibility:'none', 'text-field':['to-string',['get','people']],
        'text-font':LABEL_FONT, 'text-size':13 },
      paint:{ 'text-color':['step',['get','people'],'#7e3f0c',10,'#ffffff'] } });

    // Zoomed in: ONE dot per institution, sized by how many selected people studied
    // there. A university with 50 students is still a single dot, never 50.
    map.addLayer({ id:'edu-points', type:'circle', source:'education', filter:['!',['has','point_count']],
      layout:{ visibility:'none' },
      paint:{ 'circle-color':['step',['get','count'],'#e67e22',2,'rgba(250,215,160,0.9)',10,'rgba(230,126,34,0.92)',100,'rgba(126,63,12,0.95)'],
        'circle-radius':['step',['get','count'],5,2,17,10,21,100,25],
        'circle-opacity':0.9,
        'circle-stroke-width':['step',['get','count'],1.5,2,2],
        'circle-stroke-color':['step',['get','count'],'#2c3e50',2,'#e67e22',10,'#ca6f1e',100,'#6e3b10'] } });
    // The count is drawn only where more than one person studied there: a lone dot
    // labelled "1" is noise, and that dot is deliberately small like a birth point.
    map.addLayer({ id:'edu-count', type:'symbol', source:'education', filter:['!',['has','point_count']],
      layout:{ visibility:'none',
        'text-field':['case',['>',['get','count'],1],['to-string',['get','count']],''],
        'text-font':LABEL_FONT, 'text-size':13 },
      paint:{ 'text-color':['step',['get','count'],'#7e3f0c',10,'#ffffff'] } });

    // --- synchronized hover: birth + death + life vector all light up together ---
    map.addLayer({ id:'hover-line', type:'line', source:'hover', filter:['==','$type','LineString'],
      layout:{ 'line-cap':'round' },
      paint:{ 'line-color':'#f39c12', 'line-width':4.5, 'line-opacity':1 } });
    map.addLayer({ id:'hover-points', type:'circle', source:'hover', filter:['==','$type','Point'],
      paint:{ 'circle-color':['get','fill'], 'circle-radius':9, 'circle-opacity':1,
        'circle-stroke-width':4, 'circle-stroke-color':'#f39c12' } });

    // --- selected-person highlight (always on top) ---
    map.addLayer({ id:'highlight-line', type:'line', source:'highlight', filter:['==','$type','LineString'],
      paint:{ 'line-color':'#8e44ad', 'line-width':2.5, 'line-opacity':0.85, 'line-dasharray':[2,2] } });
    map.addLayer({ id:'highlight-points', type:'circle', source:'highlight', filter:['==','$type','Point'],
      paint:{ 'circle-color':['get','fill'], 'circle-radius':12, 'circle-opacity':0.55,
        'circle-stroke-width':4, 'circle-stroke-color':'#8e44ad' } });

    mapReady = true;
    // OHM's docs use map.once('styledata') for the first filter; by 'load' the style is
    // up, so filter here where mapReady is already true. Forced: the opening view must
    // be exact, not throttled.
    // Strip first: no point localizing or date-filtering layers that are about to go.
    stripInfrastructure();
    applyBasemapDate(true);
    localizeBasemapLabels();   // OHM's embed localizes right after filtering by date
    // Index the label layers AFTER localizing, so the recorded visibility is the
    // style's own and not something we introduced.
    captureBasemapGroups();
    applyBasemapGroupVis();    // honour the checkboxes at first paint
    window.filterMapData();
    if (pendingFilter){ pendingFilter = false; window.filterMapData(); }

    // Open on the whole world. The catalog runs from San Francisco to Japan, so opening
    // on Europe hides an eighth of it behind a pan the reader has no reason to make.
    // animate:false so this is the opening view, not a fly-in from somewhere else.
    try {
      map.fitBounds(VIEW_BOUNDS, { padding: 24, animate: false });
    } catch (e) {
      console.warn('could not fit the world view; falling back to the configured centre', e);
    }

    // Remember where the map opens, so currentState() can leave the view out of the URL
    // until the user actually moves it. Captured AFTER the fit, so the fitted world view
    // is what counts as "unmoved", and BEFORE any hash is applied.
    homeView = { lng: map.getCenter().lng, lat: map.getCenter().lat, zoom: map.getZoom() };

    // Restore the shared view LAST: sources and layers must exist before a filter pass
    // or a jumpTo can land. An empty hash leaves the default view untouched.
    var urlState = readHash();
    if (Object.keys(urlState).length) applyUrlState(urlState);

    // Panning and zooming are part of the view. moveend, not move: one write when the
    // gesture settles rather than a rewrite per frame.
    map.on('moveend', function(){ if (window.__writeUrl) window.__writeUrl(); });
  });

  // Clicking a cluster zooms to expand it (MapLibre native).
  var CLUSTER_SOURCE = { 'birth-clusters':'births', 'death-clusters':'deaths', 'edu-clusters':'education' };
  ['birth-clusters','death-clusters','edu-clusters'].forEach(function(layerId){
    var src = CLUSTER_SOURCE[layerId];
    map.on('click', layerId, function(e){
      var f = map.queryRenderedFeatures(e.point, { layers:[layerId] })[0];
      if (!f) return;
      map.getSource(src).getClusterExpansionZoom(f.properties.cluster_id).then(function(z){
        map.easeTo({ center: f.geometry.coordinates, zoom: z });
      }).catch(function(){});
    });
    map.on('mouseenter', layerId, function(){ map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', layerId, function(){ map.getCanvas().style.cursor = ''; });
  });

  // An institution dot gets a popup about the PLACE and everyone who studied there.
  map.on('click', 'edu-points', function(e){
    var f = e.features && e.features[0]; if (!f) return;
    if (popup) popup.remove();
    popup = new maplibregl.Popup({ maxWidth:'340px', closeButton:true })
      .setLngLat(f.geometry.coordinates).setHTML(buildEduPopupHtml(f.properties.key)).addTo(map);
  });
  map.on('mouseenter', 'edu-points', function(){ map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'edu-points', function(){ map.getCanvas().style.cursor = ''; });

  // Clicking an individual point opens the rich popup.
  ['birth-points','death-points','highlight-points'].forEach(function(layerId){
    map.on('click', layerId, function(e){
      var f = e.features && e.features[0]; if (!f) return;
      var p = people[f.properties.idx]; if (!p) return;
      if (window.__writeUrl) window.__writeUrl();
      if (popup) popup.remove();
      popup = new maplibregl.Popup({ maxWidth:'340px', closeButton:true })
        .setLngLat(f.geometry.coordinates).setHTML(buildPopupHtml(p)).addTo(map);
    });
    map.on('mouseenter', layerId, function(){ map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', layerId, function(){ map.getCanvas().style.cursor = ''; });
  });

  // ---------- synchronized hover ----------
  // Hovering any one of a person's elements (birth dot, death dot, life vector)
  // highlights all three in orange -- but only those whose layer is actually visible,
  // matching the old build where a hidden layer simply had nothing to light up.
  function layerVisible(id){
    if (!mapReady || !map.getLayer(id)) return false;
    return map.getLayoutProperty(id, 'visibility') !== 'none';
  }
  function hoverFC(p){
    var feats = [];
    if (layerVisible('birth-points')){
      feats.push({ type:'Feature', properties:{ fill:'#2ecc71' },
                   geometry:{ type:'Point', coordinates:[p.blon, p.blat] } });
    }
    if (layerVisible('edu-points')){
      (p.edu || []).forEach(function(st){
        feats.push({ type:'Feature', properties:{ fill:'#e67e22' },
                     geometry:{ type:'Point', coordinates:[st.lon, st.lat] } });
      });
    }
    if (p.dlat != null && layerVisible('death-points')){
      feats.push({ type:'Feature', properties:{ fill:'#e74c3c' },
                   geometry:{ type:'Point', coordinates:[p.dlon, p.dlat] } });
    }
    if (layerVisible('vector-lines')){
      var pts = lifePath(p);   // birth -> education stops -> death
      if (pts.length >= 2){
        feats.push({ type:'Feature', properties:{}, geometry:{ type:'LineString', coordinates: pts } });
      }
    }
    return { type:'FeatureCollection', features: feats };
  }
  var hoveredIdx = null;

  // A small, non-interactive tooltip shown while hovering a life vector:
  //   "Name: Birthplace -> Place of death"
  var vectorTip = null;
  function hideVectorTip(){ if (vectorTip){ vectorTip.remove(); vectorTip = null; } }
  function showVectorTip(lngLat, p){
    // The line now routes through education, so the tooltip names every stop in order.
    var chain = [clean(p.born_town) || 'Unknown'];
    (p.edu || []).forEach(function(st){ chain.push(clean(st.town) || clean(st.inst) || 'Education'); });
    if (p.dlat != null) chain.push(clean(p.died_town) || 'Unknown');
    // Collapse consecutive repeats: someone born in New York who then studied in New
    // York should read "New York -> Ann Arbor", not "New York -> New York -> Ann Arbor".
    chain = chain.filter(function(t, i){ return i === 0 || t !== chain[i - 1]; });
    var arrow = ' <span class="vt-arrow">&#8594;</span> ';
    var html = '<span class="vt-name">' + esc(p.name || 'Unknown') + ':</span> ' +
               chain.map(esc).join(arrow);
    if (!vectorTip){
      // A width cap so long birth -> education -> death chains wrap onto multiple
      // lines rather than overflowing the viewport near the map edge.
      vectorTip = new maplibregl.Popup({ closeButton:false, closeOnClick:false,
        className:'vector-tip', offset:12, maxWidth:'280px' });
    }
    vectorTip.setLngLat(lngLat).setHTML(html).addTo(map);
  }

  function setHover(idx){
    if (!mapReady || idx === hoveredIdx) return;
    hoveredIdx = idx;
    if (idx == null) hideVectorTip();
    var src = map.getSource('hover'); if (!src) return;
    src.setData((idx == null || !people[idx]) ? fcEmpty() : hoverFC(people[idx]));
  }
  window.__clearHover = function(){ setHover(null); hideVectorTip(); };

  ['birth-points','death-points','vector-lines'].forEach(function(layerId){
    map.on('mousemove', layerId, function(e){
      var f = e.features && e.features[0]; if (!f) return;
      setHover(f.properties.idx);
      // The tooltip belongs to the vector only; it tracks the cursor along the line.
      if (layerId === 'vector-lines'){
        var p = people[f.properties.idx];
        if (p) showVectorTip(e.lngLat, p);
      }
    });
    map.on('mouseleave', layerId, function(){ setHover(null); hideVectorTip(); });
  });

  // ---------- layer toggles (our own layer control) ----------
  function setVis(ids, on){
    if (!mapReady) return;
    ids.forEach(function(id){ if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none'); });
  }
  var LAYER_GROUPS = {
    'lyr-births':    ['birth-clusters','birth-cluster-count','birth-points'],
    'lyr-education': ['edu-clusters','edu-cluster-count','edu-points','edu-count'],
    'lyr-deaths':    ['death-clusters','death-cluster-count','death-points'],
    'lyr-vectors':   ['vector-lines'],
    'lyr-birthheat': ['birthheat-layer'],
    'lyr-eduheat':   ['eduheat-layer'],
    'lyr-deathheat': ['deathheat-layer']
  };
  Object.keys(LAYER_GROUPS).forEach(function(id){
    var cbx = el(id);
    if (cbx) cbx.onchange = function(){ setVis(LAYER_GROUPS[id], cbx.checked); setHover(null); if (window.__writeUrl) window.__writeUrl(); };
  });
  // Basemap labels are not one of OUR layers, so they are resolved by id against the
  // loaded style rather than through LAYER_GROUPS.
  Object.keys(GROUP_CB).forEach(function(g){
    var cbx = el(GROUP_CB[g]);
    if (cbx) cbx.onchange = function(){ applyBasemapGroupVis(); if (window.__writeUrl) window.__writeUrl(); };
  });
  map.on('load', function(){
    Object.keys(LAYER_GROUPS).forEach(function(id){
      var cbx = el(id); if (cbx) setVis(LAYER_GROUPS[id], cbx.checked);
    });
  });
"""


MAP_JS_C = r"""
  // ---------- query builder ----------
  function addPill(categoryKey, value, displayText){
    var dz = el('query-master-dropzone'), ph = el('query-drop-placeholder');
    var pid = 'active-filter-' + categoryKey + '-' + encodeURIComponent(value);
    if (document.getElementById(pid)) return;
    if (ph) ph.style.display = 'none';
    var pill = document.createElement('div');
    pill.className = 'query-pill'; pill.id = pid;
    pill.setAttribute('data-category', categoryKey);
    pill.setAttribute('data-value', value);
    pill.textContent = (labelMap[categoryKey] || categoryKey) + ': ' + displayText;
    var x = document.createElement('span'); x.className = 'qp-x'; x.innerHTML = '&times;';
    x.onclick = function(){ pill.remove(); if (dz.querySelectorAll('.query-pill').length === 0 && ph) ph.style.display = 'block'; };
    pill.appendChild(x); dz.appendChild(pill);
  }

  (function buildCategorySelects(){
    var host = el('query-category-selects');
    host.innerHTML = categoryMeta.map(function(meta){
      var labels = valueLabelMaps[meta.key] || {};
      var opts = '<option value="">-- Select --</option>' + (categories[meta.key] || []).map(function(v){
        return '<option value="' + esc(v) + '">' + esc(labels[v] || v) + '</option>';
      }).join('');
      return '<div style="margin-bottom:4px;"><b>' + esc(meta.label) + ':</b></div>' +
             '<select id="select-' + meta.key + '" class="query-select-node" data-cat="' + meta.key + '">' + opts + '</select>';
    }).join('');
    Array.prototype.forEach.call(host.querySelectorAll('select'), function(sel){
      sel.onchange = function(){
        if (!sel.value) return;
        var opt = sel.options[sel.selectedIndex];
        addPill(sel.getAttribute('data-cat'), sel.value, opt ? opt.text : sel.value);
        sel.value = '';
      };
    });
  })();

  el('btn-apply-query').onclick = function(){ window.filterMapData(); };
  el('btn-clear-query').onclick = function(){
    var dz = el('query-master-dropzone');
    Array.prototype.forEach.call(dz.querySelectorAll('.query-pill'), function(n){ n.remove(); });
    var ph = el('query-drop-placeholder'); if (ph) ph.style.display = 'block';
    categoryMeta.forEach(function(m){ var s = el('select-' + m.key); if (s) s.value = ''; });
    window.filterMapData();
  };
  Array.prototype.forEach.call(document.querySelectorAll('input[name="query-match-mode"]'), function(r){
    r.onchange = function(){ window.filterMapData(); };
  });

  // ---------- CSV export of the current selection ----------
  el('btn-export-selection').onclick = function(){
    var rows = window.__currentSelection || [];
    var fixed = [['Name','name'],['Born Year','born_year'],['Born Town','born_town'],['Born Latitude','blat'],
                 ['Born Longitude','blon'],['Died Year','died_year'],['Died Town','died_town'],
                 ['Died Latitude','dlat'],['Died Longitude','dlon']];
    function catValue(g, key){
      var v = g[key], vlm = valueLabelMaps[key];
      if (vlm && v !== null && v !== undefined && v !== '') v = vlm[v] || vlm[normNum(v)] || v;
      return v;
    }
    function f(v){ return (v===null||v===undefined) ? '' : '"' + String(v).replace(/"/g,'""') + '"'; }
    var headers = fixed.map(function(c){ return c[0]; })
      .concat(categoryMeta.map(function(m){ return m.label; }))
      .concat(noteMeta.map(function(m){ return m.label; }));
    var lines = [headers.map(f).join(',')];
    rows.forEach(function(g){
      var vals = fixed.map(function(c){ return g[c[1]]; })
        .concat(categoryMeta.map(function(m){ return catValue(g, m.key); }))
        .concat(noteMeta.map(function(m){ return g[m.key]; }));
      lines.push(vals.map(f).join(','));
    });
    var csv = '\ufeff' + lines.join('\r\n');
    var blob = new Blob([csv], { type:'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var fname = 'westfall_selection';
    if (el('temporal-enable').checked) fname += '_' + el('slider-min').value + '-' + el('slider-max').value;
    fname += '_' + rows.length + 'people.csv';
    var a = document.createElement('a'); a.href = url; a.download = fname;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
  };

  // ---------- temporal controller + playback ----------
  var cb = el('temporal-enable'), sMin = el('slider-min'), sMax = el('slider-max');
  var lMin = el('lbl-min'), lMax = el('lbl-max'), wrapper = el('slider-wrapper');
  var playYearDisplay = el('play-year-display'), playBtn = el('play-btn'), pauseBtn = el('pause-btn');
  var playSpeedSel = el('play-speed'), playbackEnable = el('playback-enable'), playControls = el('play-controls');

  // The sliders stay NUMERIC over the raw (possibly negative) years -- that is the
  // whole point of storing BC as a negative. Only the readouts are formatted.
  [sMin, sMax].forEach(function(s){ s.min = dataMinYear; s.max = dataMaxYear; });
  sMin.value = dataMinYear; sMax.value = dataMaxYear;
  lMin.textContent = fmtYear(dataMinYear); lMax.textContent = fmtYear(dataMaxYear);

  function updateYearDisplay(){
    if (!playYearDisplay) return;
    if (!cb.checked){ playYearDisplay.textContent = 'All years'; playYearDisplay.style.fontSize = '15px'; return; }
    var mn = parseInt(sMin.value), mx = parseInt(sMax.value);
    if (mn === mx){ playYearDisplay.textContent = fmtYear(mn); playYearDisplay.style.fontSize = '20px'; }
    else { playYearDisplay.textContent = fmtYearRange(mn, mx, ' \u2013 '); playYearDisplay.style.fontSize = '15px'; }
  }
  function capturePlayBounds(){ window.__playStart = parseInt(sMin.value); window.__playEnd = parseInt(sMax.value); }

  cb.onchange = function(){
    wrapper.style.display = this.checked ? 'block' : 'none';
    window.pausePlay();
    if (this.checked){
      sMin.value = dataMinYear; sMax.value = dataMaxYear;
      lMin.textContent = fmtYear(dataMinYear); lMax.textContent = fmtYear(dataMaxYear);
      capturePlayBounds();
    }
    window.__playYear = null; updateYearDisplay(); window.filterMapData();
    applyBasemapDate(true);   // enabling/disabling the timeline is a jump, not a drift
  };
  sMin.oninput = function(){
    window.pausePlay();
    if (parseInt(sMin.value) > parseInt(sMax.value)) sMin.value = sMax.value;
    lMin.textContent = fmtYear(sMin.value); capturePlayBounds(); window.__playYear = null;
    updateYearDisplay(); window.filterMapData();
  };
  sMax.oninput = function(){
    window.pausePlay();
    if (parseInt(sMax.value) < parseInt(sMin.value)) sMax.value = sMin.value;
    lMax.textContent = fmtYear(sMax.value); capturePlayBounds(); window.__playYear = null;
    updateYearDisplay(); window.filterMapData();
  };

  window.__playYear = null; window.__playStart = dataMinYear; window.__playEnd = dataMaxYear;
  window.__playTimer = null; window.__playSpeed = 1.0;
  var PLAY_STEP = 1, PLAY_BASE_INTERVAL = 200;

  function setTimelineYear(y){
    sMin.value = y; sMax.value = y; lMin.textContent = fmtYear(y); lMax.textContent = fmtYear(y);
    window.__playYear = y; updateYearDisplay(); window.filterMapData();
  }
  function playTick(){
    var ny = window.__playYear + PLAY_STEP;
    if (ny >= window.__playEnd){ setTimelineYear(window.__playEnd); window.pausePlay(); return; }
    setTimelineYear(ny);
  }
  function startPlayTimer(){ window.__playTimer = setInterval(playTick, PLAY_BASE_INTERVAL / window.__playSpeed); }
  window.startPlay = function(){
    if (window.__playTimer) return;
    if (!cb.checked){
      cb.checked = true; wrapper.style.display = 'block';
      sMin.value = dataMinYear; sMax.value = dataMaxYear;
      lMin.textContent = fmtYear(dataMinYear); lMax.textContent = fmtYear(dataMaxYear);
      capturePlayBounds(); window.__playYear = null;
    }
    if (window.__playYear === null || window.__playYear >= window.__playEnd) window.__playYear = window.__playStart;
    setTimelineYear(window.__playYear); startPlayTimer();
  };
  window.pausePlay = function(){
    if (window.__playTimer){ clearInterval(window.__playTimer); window.__playTimer = null; }
    // Throttling may have left the basemap a few years behind the displayed year.
    // Once motion stops, snap it to the exact year being shown.
    if (window.__applyBasemapDate) window.__applyBasemapDate(true);
  };
  if (playBtn) playBtn.onclick = window.startPlay;
  if (pauseBtn) pauseBtn.onclick = window.pausePlay;
  if (playSpeedSel) playSpeedSel.onchange = function(){
    window.__playSpeed = parseFloat(this.value) || 1.0;
    if (window.__playTimer){ clearInterval(window.__playTimer); startPlayTimer(); }
  };
  if (playbackEnable) playbackEnable.onchange = function(){
    if (playControls) playControls.style.display = this.checked ? 'block' : 'none';
    if (!this.checked) window.pausePlay();
  };
  updateYearDisplay();
"""


MAP_JS_D = r"""
  // ---------- person search + highlight ----------
  function highlightFC(p){
    var feats = [];
    feats.push({ type:'Feature', properties:{ idx:p.idx, fill:'#2ecc71' },
                 geometry:{ type:'Point', coordinates:[p.blon, p.blat] } });
    (p.edu || []).forEach(function(st){
      feats.push({ type:'Feature', properties:{ idx:p.idx, fill:'#e67e22' },
                   geometry:{ type:'Point', coordinates:[st.lon, st.lat] } });
    });
    if (p.dlat != null){
      feats.push({ type:'Feature', properties:{ idx:p.idx, fill:'#e74c3c' },
                   geometry:{ type:'Point', coordinates:[p.dlon, p.dlat] } });
    }
    // The highlighted journey follows the same routed path as the life vector.
    var pts = lifePath(p);
    if (pts.length >= 2){
      feats.push({ type:'Feature', properties:{ idx:p.idx },
                   geometry:{ type:'LineString', coordinates: pts } });
    }
    return { type:'FeatureCollection', features: feats };
  }

  window.clearPersonHighlight = function(){
    selectedIdx = null;
    if (mapReady && map.getSource('highlight')) map.getSource('highlight').setData(fcEmpty());
    if (popup){ popup.remove(); popup = null; }
    var btn = el('clear-sel-btn'); if (btn) btn.style.display = 'none';
    var inp = el('person-search-input'), clr = el('person-search-clear');
    if (inp) inp.value = '';
    if (clr) clr.style.display = 'none';
    if (window.__writeUrl) window.__writeUrl();
  };

  // Which person is highlighted. Nothing tracked this before: selectPerson painted the
  // highlight and forgot. The permalink needs to name the selection, so record it here
  // and clear it in clearPersonHighlight -- the only two places it changes.
  var selectedIdx = null;
  window.__selectedIdx = function(){ return selectedIdx; };

  window.selectPerson = function(index){
    var p = people[index]; if (!p) return;
    selectedIdx = index;
    function doSelect(){
      map.getSource('highlight').setData(highlightFC(p));
      var pts = lifePath(p);   // birth -> education -> death
      if (pts.length > 1){
        var b = new maplibregl.LngLatBounds(pts[0], pts[0]);
        pts.forEach(function(c){ b.extend(c); });
        map.fitBounds(b, { padding: 120, maxZoom: 9 });
      } else {
        map.easeTo({ center:[p.blon, p.blat], zoom: Math.max(map.getZoom(), 6) });
      }
      if (popup) popup.remove();
      popup = new maplibregl.Popup({ maxWidth:'340px', closeButton:true })
        .setLngLat([p.blon, p.blat]).setHTML(buildPopupHtml(p)).addTo(map);
      var btn = el('clear-sel-btn'); if (btn) btn.style.display = 'block';
    }
    if (mapReady) doSelect(); else map.on('load', doSelect);
  };

  var psInput = el('person-search-input'), psSugg = el('person-search-suggestions'), psClear = el('person-search-clear');
  // Lowercased once at build, not per keystroke: at 25x this list is ~9,000 people and
  // the matcher runs on every character typed.
  var peopleIndex = people.map(function(p){
      var alt = p.alt || [];
      return { i:p.idx, name:p.name || '', year:p.born_year, alt:alt,
               lname:(p.name || '').toLowerCase(),
               lalt:alt.map(function(a){ return a.toLowerCase(); }) };
    })
    .filter(function(p){ return p.name && p.name.trim() !== ''; })
    .sort(function(a,b){ return a.name.localeCompare(b.name); });

  // A figure is findable by the catalog's name OR by any name it also records for them.
  // Returns which ALIAS matched, so the suggestion can show why a search for "Abelard"
  // is offering "Pierre Abailard" -- an unexplained result looks like a bug.
  function personMatch(p, q){
    if (p.lname.indexOf(q) !== -1) return { hit:true, via:null };
    for (var j = 0; j < p.lalt.length; j++){
      if (p.lalt[j].indexOf(q) !== -1) return { hit:true, via:p.alt[j] };
    }
    return { hit:false, via:null };
  }

  function renderPersonSuggestions(q){
    q = q.trim().toLowerCase();
    psClear.style.display = q ? 'block' : 'none';
    if (!q){ psSugg.style.display = 'none'; psSugg.innerHTML = ''; return; }
    var matches = [];
    for (var k = 0; k < peopleIndex.length && matches.length < 50; k++){
      var m = personMatch(peopleIndex[k], q);
      if (m.hit) matches.push({ p: peopleIndex[k], via: m.via });
    }
    // A name match outranks an alias match: someone typing "Abbe" wants Cleveland Abbe
    // before figures who merely have "Abbe" buried in an alternate form.
    matches.sort(function(a, b){ return (a.via ? 1 : 0) - (b.via ? 1 : 0); });
    if (!matches.length){
      psSugg.innerHTML = '<div style="padding:6px 7px; color:#95a5a6; font-size:11px;">No matches</div>';
      psSugg.style.display = 'block'; return;
    }
    psSugg.innerHTML = matches.map(function(m){
      var p = m.p;
      var yr = (p.year !== null && p.year !== undefined) ? ' <span style="color:#95a5a6;">(b. ' + esc(fmtYear(p.year)) + ')</span>' : '';
      var via = m.via ? '<span class="ps-alt">also: ' + esc(m.via) + '</span>' : '';
      return '<div class="person-suggestion" data-idx="' + p.i + '" data-name="' + esc(p.name) + '">' +
             esc(p.name) + yr + via + '</div>';
    }).join('');
    psSugg.style.display = 'block';
    Array.prototype.forEach.call(psSugg.querySelectorAll('.person-suggestion'), function(node){
      node.onmousedown = function(ev){ ev.preventDefault(); };
      node.onclick = function(){
        psInput.value = node.getAttribute('data-name');
        psSugg.style.display = 'none'; psClear.style.display = 'block';
        window.selectPerson(parseInt(node.getAttribute('data-idx')));
      };
    });
  }
  psInput.oninput = function(){ renderPersonSuggestions(psInput.value); };
  psInput.onfocus = function(){ if (psInput.value.trim()) renderPersonSuggestions(psInput.value); };
  psInput.onblur = function(){ setTimeout(function(){ psSugg.style.display = 'none'; }, 150); };
  psInput.onkeydown = function(ev){
    if (ev.key === 'Enter'){ var f = psSugg.querySelector('.person-suggestion'); if (f){ ev.preventDefault(); f.onclick(); } }
    else if (ev.key === 'Escape'){ psSugg.style.display = 'none'; }
  };
  psClear.onclick = function(){
    psInput.value = ''; psSugg.style.display = 'none'; psClear.style.display = 'none';
    window.clearPersonHighlight(); psInput.focus();
  };
  el('clear-sel-btn').onclick = function(){ window.clearPersonHighlight(); };

  // The tab is part of the view too. dashShowPage lives in the shell script, so wrap it
  // rather than reaching into it.
  (function(){
    var orig = window.dashShowPage;
    if (typeof orig !== 'function') return;
    window.dashShowPage = function(){
      var r = orig.apply(this, arguments);
      if (window.__writeUrl) window.__writeUrl();
      return r;
    };
  })();

  // ---------- permalink: the current view, encoded in the URL ----------
  // Everything that makes a view distinct goes in the hash, so a link reproduces what
  // the sender was actually looking at: the year window, the active filters, the map
  // position, the selected person, which layers are on, and which tab.
  //
  // The hash, not the query string: it survives file:// and never hits a server.
  //
  // Two choices worth spelling out:
  //
  //  * The selected person travels by NAME, not by index. An index is shorter, but it
  //    is a position in the current build -- rebuild the catalog with one row inserted
  //    and every old link silently points at the WRONG person. A name either resolves
  //    or it does not, and it tells a reader what the link is for.
  //  * Filters travel as category:value using the canonical value. If a later build
  //    drops that value, the filter is skipped rather than faked.
  var URL_KEYS = ['t', 'y', 'f', 'm', 'v', 'p', 'l'];
  var applyingUrl = false;      // guards against apply -> write -> apply loops
  var urlWriteTimer = null;
  var homeView = null;          // the view the map opens at; see currentState()

  var LAYER_IDS = ['births', 'education', 'deaths', 'vectors', 'birthheat', 'eduheat', 'deathheat',
                   'basemap-labels', 'basemap-borders'];
  var LAYER_DEFAULT = { births: true, 'basemap-labels': true, 'basemap-borders': true };

  // Short codes keep the layer list from dominating the URL: 'births,vectors,
  // basemap-labels' is 29 characters; 'b,v,l' is five.
  var LAYER_CODE = { births:'b', education:'e', deaths:'d', vectors:'v',
                     birthheat:'bh', eduheat:'eh', deathheat:'dh',
                     'basemap-labels':'l', 'basemap-borders':'bo' };
  var CODE_LAYER = {}; Object.keys(LAYER_CODE).forEach(function(k){ CODE_LAYER[LAYER_CODE[k]] = k; });
  var CODE_CAT = {};   Object.keys(CAT_CODE).forEach(function(k){ CODE_CAT[CAT_CODE[k]] = k; });

  // encodeURIComponent escapes ',' ':' and ' ', none of which need escaping inside a
  // URI fragment -- RFC 3986 allows sub-delims and ':' in pchar. Only '&' and '=' must
  // stay encoded, because those are our own separators. This alone takes ~14 characters
  // off a typical link and makes it readable at a glance.
  function enc(v){
    return encodeURIComponent(v)
      .replace(/%2C/gi, ',')
      .replace(/%3A/gi, ':')
      .replace(/%20/g, '+');   // readHash turns '+' back into a space
  }

  // Coordinate precision is pointless past what the zoom can show: at z3 a whole degree
  // is a few pixels. Scale the decimals to the zoom and drop trailing zeros.
  function trimCoord(n, dp){
    var v = parseFloat(Number(n).toFixed(dp));
    return String(v);
  }

  function readHash(){
    var h = (location.hash || '').replace(/^#/, '');
    var out = {};
    if (!h) return out;
    h.split('&').forEach(function(part){
      var i = part.indexOf('=');
      if (i < 1) return;
      var k = part.slice(0, i);
      if (URL_KEYS.indexOf(k) === -1) return;
      try { out[k] = decodeURIComponent(part.slice(i + 1).replace(/\+/g, ' ')); } catch (e){}
    });
    return out;
  }

  function currentState(){
    var st = {};

    var active = document.querySelector('.dash-page.dash-active');
    if (active && active.id !== 'page-map') st.t = active.id.replace('page-', '');

    if (el('temporal-enable').checked) st.y = el('slider-min').value + ',' + el('slider-max').value;

    var pills = currentPills();
    if (pills.length){
      st.f = pills.map(function(f){ return (CAT_CODE[f.category] || f.category) + ':' + f.value; }).join(';');
      if (matchMode() === 'or') st.m = 'or';
    }

    // Only record the view once it has MOVED. A link to "the 17th century" should be
    // #y=1600,1700, not #y=1600,1700&v=10,30,3 restating the opening position.
    if (mapReady && map){
      var c = map.getCenter(), z = map.getZoom();
      var moved = !homeView ||
                  Math.abs(c.lng - homeView.lng) > 1e-4 ||
                  Math.abs(c.lat - homeView.lat) > 1e-4 ||
                  Math.abs(z - homeView.zoom) > 0.01;
      if (moved){
        var dp = z < 8 ? 2 : (z < 12 ? 3 : 4);
        st.v = trimCoord(c.lng, dp) + ',' + trimCoord(c.lat, dp) + ',' + trimCoord(z, 1);
      }
    }

    var si = window.__selectedIdx ? window.__selectedIdx() : null;
    if (si != null && people[si]) st.p = people[si].name;

    // Only record layers when they differ from the defaults, so a plain view stays a
    // plain URL.
    var on = LAYER_IDS.filter(function(id){ var c = el('lyr-' + id); return c && c.checked; })
                      .map(function(id){ return LAYER_CODE[id] || id; });
    var isDefault = LAYER_IDS.every(function(id){
      var c = el('lyr-' + id);
      return !c || c.checked === !!LAYER_DEFAULT[id];
    });
    if (!isDefault) st.l = on.join(',');

    return st;
  }

  function writeUrl(){
    if (applyingUrl) return;
    clearTimeout(urlWriteTimer);
    // Map moves fire continuously; coalesce so the address bar is not rewritten 60x/sec.
    urlWriteTimer = setTimeout(function(){
      var st = currentState();
      var h = URL_KEYS.filter(function(k){ return st[k] != null && st[k] !== ''; })
                      .map(function(k){ return k + '=' + enc(st[k]); })
                      .join('&');
      // replaceState, not pushState: panning the map should not fill the back button.
      try { history.replaceState(null, '', h ? '#' + h : location.pathname + location.search); }
      catch (e){ /* file:// in some browsers refuses replaceState; the app is unaffected */ }
    }, 250);
  }
  window.__readHash = readHash;   // tests round-trip through the REAL parser, not a copy
  window.__writeUrl = writeUrl;
  window.__currentUrlState = currentState;

  // Recover a pill's canonical value and human label from the select that already
  // lists every option, rather than trusting the URL's casing.
  function addPillFromUrl(cat, value){
    var sel = el('select-' + cat);
    if (!sel) return false;
    for (var i = 0; i < sel.options.length; i++){
      var o = sel.options[i];
      if (o.value && o.value.toLowerCase() === String(value).toLowerCase()){
        addPill(cat, o.value, o.text);
        return true;
      }
    }
    return false;   // value no longer in the data: skip it rather than invent a pill
  }

  function applyUrlState(st){
    applyingUrl = true;
    try {
      // layers first: they only change visibility, and doing them before the filter
      // pass means a single redraw
      if (st.l != null){
        // accept short codes and full names alike, so nothing already copied breaks
        var want = (st.l ? st.l.split(',') : []).map(function(x){ return CODE_LAYER[x] || x; });
        LAYER_IDS.forEach(function(id){
          var c = el('lyr-' + id);
          if (!c) return;
          var next = want.indexOf(id) !== -1;
          if (c.checked !== next){ c.checked = next; if (c.onchange) c.onchange(); }
        });
      }

      if (st.f){
        st.f.split(';').forEach(function(tok){
          var i = tok.indexOf(':');
          if (i > 0){
            var cat = tok.slice(0, i);
            addPillFromUrl(CODE_CAT[cat] || cat, tok.slice(i + 1));
          }
        });
      }
      if (st.m === 'or'){
        var r = document.querySelector('input[name="query-match-mode"][value="or"]');
        if (r) r.checked = true;
      }

      if (st.y){
        var yy = st.y.split(',');
        var mn = parseInt(yy[0], 10), mx = parseInt(yy[1], 10);
        if (!isNaN(mn) && !isNaN(mx)){
          var cbx = el('temporal-enable');
          cbx.checked = true;
          if (cbx.onchange) cbx.onchange();        // reveals the panel, resets to full range
          el('slider-min').value = mn; el('slider-max').value = mx;
          if (el('slider-max').oninput) el('slider-max').oninput();
        }
      }

      window.filterMapData();

      if (st.v && mapReady && map){
        var v = st.v.split(',').map(parseFloat);
        if (v.length === 3 && v.every(function(n){ return !isNaN(n); })){
          map.jumpTo({ center: [v[0], v[1]], zoom: v[2] });
        }
      }

      if (st.p){
        // by name, case-insensitively; first match wins. Unknown -> no selection.
        var target = null;
        for (var i = 0; i < people.length; i++){
          if (people[i].name.toLowerCase() === st.p.toLowerCase()){ target = people[i].idx; break; }
        }
        if (target != null) window.selectPerson(target);
      }

      if (st.t && st.t !== 'map'){
        // Call dashShowPage directly rather than clicking the tab: the click path runs
        // through an inline onclick attribute, so this does not depend on how the
        // button happens to be wired.
        var pid = 'page-' + st.t;
        var btn = document.querySelector('#dash-nav .dash-tab[data-page="' + pid + '"]');
        if (btn && typeof window.dashShowPage === 'function') window.dashShowPage(pid, btn);
      }
    } catch (e){
      console.warn('could not restore the view from the URL', e);
    } finally {
      applyingUrl = false;
    }
  }
  window.__applyUrlState = applyUrlState;

  // Copy link. The address bar already holds the permalink, but a button says so out
  // loud -- and on mobile the URL is often not visible at all.
  (function(){
    var btn = el('copy-link');
    if (!btn) return;
    btn.onclick = function(){
      // Flush any pending debounced write so the copied URL is the CURRENT view.
      clearTimeout(urlWriteTimer);
      var st = currentState();
      var h = URL_KEYS.filter(function(k){ return st[k] != null && st[k] !== ''; })
                      .map(function(k){ return k + '=' + enc(st[k]); }).join('&');
      var url = location.origin + location.pathname + location.search + (h ? '#' + h : '');
      var done = function(okFlag){
        var was = btn.textContent;
        btn.textContent = okFlag ? 'Copied' : 'Press Ctrl+C';
        btn.classList.add('dash-copy-done');
        setTimeout(function(){ btn.textContent = was; btn.classList.remove('dash-copy-done'); }, 1600);
      };
      // navigator.clipboard needs a secure context; file:// and plain http do not have
      // one, so fall back to selecting the text for the user to copy themselves.
      if (navigator.clipboard && window.isSecureContext){
        navigator.clipboard.writeText(url).then(function(){ done(true); }, function(){ fallback(); });
      } else { fallback(); }

      function fallback(){
        var ta = document.createElement('textarea');
        ta.value = url;
        ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;';
        document.body.appendChild(ta); ta.focus(); ta.select();
        var okFlag = false;
        try { okFlag = document.execCommand('copy'); } catch (e){}
        document.body.removeChild(ta);
        done(okFlag);
      }
    };
  })();

  // ---------- education timeline: open / close wiring ----------
  // Delegated, because the "View timeline" button lives inside a MapLibre popup that is
  // created and destroyed on every click -- there is no stable node to bind to.
  document.addEventListener('click', function(ev){
    var t = ev.target;
    var btn = t && t.closest ? t.closest('.mp-tl-btn') : null;
    if (btn){
      ev.preventDefault();
      window.openEduTimeline(btn.getAttribute('data-key'));
    }
  });
  (function(){
    var modal = el('edu-timeline'), closeBtn = el('etl-close');
    if (closeBtn) closeBtn.onclick = function(){ window.closeEduTimeline(); };
    if (modal) modal.onclick = function(ev){
      if (ev.target === modal) window.closeEduTimeline();   // backdrop click only
    };
    document.addEventListener('keydown', function(ev){
      if (ev.key === 'Escape' && modal && modal.classList.contains('etl-open')) window.closeEduTimeline();
    });
  })();

  // ---------- collapsible cards (start collapsed so the map is unobstructed) ----------
  Array.prototype.forEach.call(document.querySelectorAll('.ui-card-header[data-toggle]'), function(h){
    var body = h.nextElementSibling;
    var chev = h.querySelector('.ui-chevron');
    if (body){ body.classList.add('ui-collapsed'); if (chev) chev.style.transform = 'rotate(-90deg)'; }
    h.onclick = function(){
      if (!body) return;
      var nowCollapsed = body.classList.toggle('ui-collapsed');
      if (chev) chev.style.transform = nowCollapsed ? 'rotate(-90deg)' : 'rotate(0deg)';
    };
  });

  // Recompute canvas size when the Map tab is re-shown (it is hidden while on Data/Info).
  window.dashOnShow = window.dashOnShow || {};
  window.dashOnShow['page-map'] = function(){ if (map) map.resize(); };

  // Seed hubs + selection immediately, before the style finishes loading.
  window.filterMapData();
})();
</script>"""


# ============================ PRESENTATION LAYER: SHELL ============================
SHELL_CSS = """<style>
:root { --dash-nav-h: 54px; }
html, body { margin:0; padding:0; height:100%; }
body { display:flex; flex-direction:column; overflow:hidden; background:#f4f6f8;
    font-family:"Helvetica Neue", Arial, sans-serif; }
#dash-nav { flex:0 0 var(--dash-nav-h); height:var(--dash-nav-h); display:flex; align-items:stretch;
    background:#1f2d3a; box-shadow:0 2px 6px rgba(0,0,0,0.25); z-index:3000; }
.dash-actions { display:flex; align-items:center; padding:0 10px; }
.dash-copy { background:transparent; border:1px solid rgba(255,255,255,0.28); color:#dfe6ec;
    font:inherit; font-size:12px; padding:5px 11px; border-radius:4px; cursor:pointer;
    white-space:nowrap; transition:background .15s, border-color .15s; }
.dash-copy:hover { background:rgba(255,255,255,0.10); border-color:rgba(255,255,255,0.5); }
.dash-copy:focus-visible { outline:2px solid #8e44ad; outline-offset:1px; }
.dash-copy-done { background:#27ae60; border-color:#27ae60; color:#fff; }
#dash-nav .dash-brand { display:flex; flex-direction:column; justify-content:center; padding:0 20px;
    color:#fff; border-right:1px solid rgba(255,255,255,0.08); }
#dash-nav .dash-brand-title { font-size:15px; font-weight:700; letter-spacing:0.2px; }
#dash-nav .dash-brand-sub { font-size:10.5px; color:#8fa3b5; margin-top:1px; }
.dash-beta { display:inline-block; margin-left:8px; padding:1px 6px; border-radius:3px;
    background:#e67e22; color:#fff; font-size:9px; font-weight:700; letter-spacing:0.5px; vertical-align:middle; }
#dash-nav .dash-tabs { display:flex; align-items:stretch; margin-left:6px; }
#dash-nav .dash-tab { display:flex; align-items:center; padding:0 24px; height:100%; color:#b8c4d0;
    font-size:13.5px; font-weight:600; cursor:pointer; border:none; background:transparent;
    border-bottom:3px solid transparent; transition:color .15s, background .15s, border-color .15s; }
#dash-nav .dash-tab:hover { color:#fff; background:rgba(255,255,255,0.05); }
#dash-nav .dash-tab.dash-tab-active { color:#fff; border-bottom-color:#2ecc71; background:rgba(255,255,255,0.06); }
#dash-nav .dash-feedback { margin-left:auto; align-self:center; margin-right:16px; display:inline-flex;
    align-items:center; gap:5px; padding:7px 16px; border-radius:4px; background:#2ecc71; color:#fff;
    font-size:13px; font-weight:700; text-decoration:none; transition:background .15s; cursor:pointer; }
#dash-nav .dash-feedback:hover { background:#27ae60; }
#dash-content { position:relative; flex:1 1 auto; min-height:0; }
.dash-page { position:absolute; top:0; right:0; bottom:0; left:0; display:none; }
.dash-page.dash-active { display:block; }
#page-data, #page-index, #page-info { overflow:auto; }
.dash-blank { height:100%; box-sizing:border-box; display:flex; align-items:center; justify-content:center;
    color:#aab4be; font-size:15px; text-align:center; }
.info-wrap { max-width:1060px; margin:0 auto; padding:30px 26px 80px; display:flex; gap:36px; align-items:flex-start; }
.info-toc { position:sticky; top:24px; flex:0 0 188px; }
.info-toc-title { text-transform:uppercase; letter-spacing:0.6px; font-size:10px; color:#95a5a6; margin:0 0 10px 12px; }
.info-toc a { display:block; padding:7px 12px; border-left:2px solid #e3e7eb; color:#5a6b7a;
    text-decoration:none; font-size:13px; transition:color .12s, border-color .12s, background .12s; }
.info-toc a:hover { color:#2c3e50; border-left-color:#8e44ad; background:#f4f6f8; }
.info-main { flex:1 1 auto; min-width:0; }
.info-head { margin-bottom:24px; }
.info-head h1 { margin:0 0 4px; font-size:24px; color:#2c3e50; }
.info-head .info-sub { color:#7f8c8d; font-size:14px; }
.info-section { background:#fff; border:1px solid #e3e7eb; border-radius:8px; padding:22px 26px;
    margin-bottom:18px; scroll-margin-top:18px; }
.info-section h2 { margin:0 0 14px; font-size:17px; color:#2c3e50; padding-bottom:9px; border-bottom:1px solid #eef1f4; }
.info-body { color:#3c4a57; font-size:14px; line-height:1.75; }
.info-body p { margin:0 0 12px; } .info-body p:last-child { margin-bottom:0; }
.info-body a { color:#8e44ad; }
.info-placeholder { color:#9aa7b2; font-style:italic; font-size:13px; background:#fafbfc;
    border:1px dashed #cfd8e0; border-radius:6px; padding:16px 18px; }
@media (max-width:720px){ .info-wrap { flex-direction:column; } .info-toc { position:static; flex-basis:auto; } }

/* ----- Index tab (dictionary of tools + terms) ----- */
#page-index { overflow:auto; }
.idx-wrap { max-width:1060px; margin:0 auto; padding:30px 26px 80px; display:flex; gap:36px; align-items:flex-start; }
.idx-toc { position:sticky; top:24px; flex:0 0 200px; }
.idx-toc-title { text-transform:uppercase; letter-spacing:0.6px; font-size:10px; color:#95a5a6; margin:0 0 10px 12px; }
.idx-toc a { display:block; padding:7px 12px; border-left:2px solid #e3e7eb; color:#5a6b7a;
    text-decoration:none; font-size:13px; transition:color .12s, border-color .12s, background .12s; }
.idx-toc a:hover { color:#2c3e50; border-left-color:#8e44ad; background:#f4f6f8; }
.idx-main { flex:1 1 auto; min-width:0; }
.idx-head { margin-bottom:24px; }
.idx-head h1 { margin:0 0 4px; font-size:24px; color:#2c3e50; }
.idx-sub { color:#7f8c8d; font-size:14px; }
.idx-intro { color:#3c4a57; font-size:14px; line-height:1.75; margin-top:12px; }
#idx-search { margin-top:16px; width:100%; max-width:360px; box-sizing:border-box; padding:8px 11px;
    border:1px solid #bdc3c7; border-radius:4px; font-size:13px; color:#2c3e50; }
#idx-search:focus { outline:2px solid #8e44ad; outline-offset:1px; border-color:#8e44ad; }
.idx-noresults { margin-top:14px; color:#9aa7b2; font-style:italic; font-size:13px; }
.idx-section { background:#fff; border:1px solid #e3e7eb; border-radius:8px; padding:22px 26px;
    margin-bottom:18px; scroll-margin-top:18px; }
.idx-section h2 { margin:0 0 6px; font-size:17px; color:#2c3e50; padding-bottom:9px; border-bottom:1px solid #eef1f4; }
.idx-blurb { margin:10px 0 4px; color:#95a5a6; font-size:12.5px; font-style:italic; }
.idx-list { margin:0; }
.idx-entry { padding:12px 0; border-bottom:1px solid #f2f4f6; }
.idx-entry:last-child { border-bottom:none; }
.idx-term { margin:0 0 4px; font-size:13.5px; font-weight:700; color:#2c3e50; }
.idx-def { margin:0; color:#3c4a57; font-size:13.5px; line-height:1.7; }
.idx-def p { margin:0 0 10px; } .idx-def p:last-child { margin-bottom:0; }
.idx-def a { color:#8e44ad; }
.idx-def code { background:#f4f6f8; border:1px solid #e3e7eb; border-radius:3px; padding:1px 5px; font-size:12px; }
.idx-todo { color:#9aa7b2; font-style:italic; font-size:12.5px; background:#fafbfc;
    border:1px dashed #cfd8e0; border-radius:5px; padding:6px 10px; display:inline-block; }
.idx-entry-empty .idx-term { color:#7a8894; }
.idx-wrap-empty { display:block; }
.idx-placeholder { color:#9aa7b2; font-style:italic; font-size:13px; background:#fafbfc;
    border:1px dashed #cfd8e0; border-radius:6px; padding:16px 18px; margin-top:20px; }
.idx-placeholder code { background:#f4f6f8; border:1px solid #e3e7eb; border-radius:3px;
    padding:1px 5px; font-size:12px; font-style:normal; }
@media (max-width:720px){ .idx-wrap { flex-direction:column; } .idx-toc { position:static; flex-basis:auto; } }
</style>"""

TAB_SCRIPT = """<script>
function dashShowPage(pageId, btn){
  var pages = document.querySelectorAll('.dash-page');
  for (var i=0;i<pages.length;i++) pages[i].classList.remove('dash-active');
  var t = document.getElementById(pageId); if (t) t.classList.add('dash-active');
  var tabs = document.querySelectorAll('#dash-nav .dash-tab');
  for (var j=0;j<tabs.length;j++) tabs[j].classList.remove('dash-tab-active');
  if (btn) btn.classList.add('dash-tab-active');
  if (window.dashOnShow && typeof window.dashOnShow[pageId] === 'function'){
    setTimeout(function(){ window.dashOnShow[pageId](); }, 0);
  }
}
window.dashShowPage = dashShowPage;

// Index tab: live filter across every term. Hides empty sections as you type.
(function(){
  function initIndexSearch(){
    var box = document.getElementById('idx-search');
    if (!box) return;   // Index tab parked behind its "coming soon" toggle
    var none = document.getElementById('idx-noresults');
    box.oninput = function(){
      var q = (box.value || '').trim().toLowerCase();
      var shown = 0;
      var sections = document.querySelectorAll('#page-index .idx-section');
      for (var i = 0; i < sections.length; i++){
        // A section HEADING is a search term too. "Advanced Query Builder" is the name
        // of a panel and of a section here, but of no single entry -- matching entries
        // alone would answer "no terms match" while a section of that exact name sat
        // right there. A heading hit shows the whole section.
        var h2 = sections[i].querySelector('h2');
        var blurb = sections[i].querySelector('.idx-blurb');
        var headText = ((h2 ? h2.textContent : '') + ' ' + (blurb ? blurb.textContent : '')).toLowerCase();
        var headHit = !!q && headText.indexOf(q) !== -1;

        var entries = sections[i].querySelectorAll('.idx-entry');
        var visible = 0;
        for (var j = 0; j < entries.length; j++){
          var term = entries[j].getAttribute('data-term') || '';
          var text = (entries[j].textContent || '').toLowerCase();
          var hit = !q || headHit || term.indexOf(q) !== -1 || text.indexOf(q) !== -1;
          entries[j].style.display = hit ? '' : 'none';
          if (hit) visible++;
        }
        sections[i].style.display = visible ? '' : 'none';
        shown += visible;
      }
      if (none) none.style.display = shown ? 'none' : 'block';
    };
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initIndexSearch);
  else initIndexSearch();
})();
</script>"""


def build_info_page():
    if INFO_TAB_COMING_SOON:
        return '<div id="page-info" class="dash-page"><div class="dash-blank">Info Tab Coming Soon</div></div>'
    sections = [
        ('info-about', 'About', INFO_ABOUT, 'Describe the project, the dataset, and its scope here.'),
        ('info-how-to-use', 'How to Use', INFO_HOW_TO_USE, 'Explain how to navigate the Map and Data tabs \u2014 filters, the timeline, search, and charts.'),
        ('info-how-to-cite', 'How to Cite', INFO_HOW_TO_CITE, 'Provide a recommended citation for this resource.'),
        ('info-works-cited', 'Works Cited', INFO_WORKS_CITED, 'List the sources, datasets, and literature this project draws on.'),
        ('info-copyright', 'Copyright', INFO_COPYRIGHT, 'State the license and copyright terms for the data and the site.'),
        ('info-ai-disclosure', 'AI Disclosure', INFO_AI_DISCLOSURE, 'Disclose how AI tools were used in compiling the data and building this site.'),
    ]
    toc = ''.join('<a href="#' + sid + '">' + html_lib.escape(t) + '</a>' for sid, t, _c, _h in sections)
    blocks = ''
    for sid, title, content, hint in sections:
        body = content.strip() if (content and content.strip()) else \
            ('<div class="info-placeholder">' + html_lib.escape(hint) + '</div>')
        blocks += ('<section id="' + sid + '" class="info-section"><h2>' + html_lib.escape(title) +
                   '</h2><div class="info-body">' + body + '</div></section>')
    return ('<div id="page-info" class="dash-page"><div class="info-wrap">'
            '<nav class="info-toc"><div class="info-toc-title">On this page</div>' + toc + '</nav>'
            '<div class="info-main"><div class="info-head"><h1>' + html_lib.escape(MAP_TITLE) + '</h1>'
            '<div class="info-sub">' + html_lib.escape(MAP_SUBTITLE) + '</div></div>' + blocks +
            '</div></div></div>')


def build_index_page():
    """Dictionary of tools and terms, generated from INDEX_GROUPS. Empty definitions
    render as 'to be added'. With no groups defined, the page shows a short note
    rather than an empty scaffold of controls that do nothing."""
    if INDEX_TAB_COMING_SOON:
        return '<div id="page-index" class="dash-page"><div class="dash-blank">Index Tab Coming Soon</div></div>'

    head = ('<div class="idx-head"><h1>Index</h1>'
            '<div class="idx-sub">A dictionary of the tools and terms used across this dashboard.</div>'
            + ('<div class="idx-intro">' + INDEX_INTRO + '</div>' if INDEX_INTRO.strip() else ''))

    if not INDEX_GROUPS:
        return ('<div id="page-index" class="dash-page"><div class="idx-wrap idx-wrap-empty">'
                '<div class="idx-main">' + head + '</div>'
                '<div class="idx-placeholder">No entries yet. '
                'Add them to <code>INDEX_GROUPS</code> in <code>build_site.py</code>.</div>'
                '</div></div></div>')

    toc = ''.join('<a href="#' + gid + '">' + title + '</a>'
                  for gid, title, _blurb, _terms in INDEX_GROUPS)

    blocks = ''
    for gid, title, blurb, terms in INDEX_GROUPS:
        entries = ''
        for term, definition in terms:
            has = bool(definition and definition.strip())
            body = definition.strip() if has else '<span class="idx-todo">definition to be added</span>'
            entries += ('<div class="idx-entry' + ('' if has else ' idx-entry-empty') + '" '
                        'data-term="' + html_lib.escape(term.lower()) + '">'
                        '<dt class="idx-term">' + term + '</dt>'
                        '<dd class="idx-def">' + body + '</dd></div>')
        blocks += ('<section id="' + gid + '" class="idx-section">'
                   '<h2>' + title + '</h2>'
                   + ('<p class="idx-blurb">' + blurb + '</p>' if blurb else '') +
                   '<dl class="idx-list">' + entries + '</dl></section>')

    return (
        '<div id="page-index" class="dash-page">'
        '<div class="idx-wrap">'
        '<nav class="idx-toc"><div class="idx-toc-title">Sections</div>' + toc + '</nav>'
        '<div class="idx-main">'
        + head +
        '<input id="idx-search" type="text" placeholder="Filter terms\u2026" autocomplete="off">'
        '<div id="idx-noresults" class="idx-noresults" style="display:none;">No terms match that filter.</div>'
        '</div>'
        + blocks +
        '</div></div></div>'
    )


def _load_dates_plugin():
    """Read the vendored maplibre-gl-dates plugin (CC0) for inlining.

    Vendored rather than pulled from a CDN for two reasons: the unscoped
    `maplibre-gl-dates` npm package was published and unpublished within the hour, so
    that documented install path is dead; and inlining keeps index.html a single file.

    Only needed for the OHM basemap. If the file is missing we fail loudly rather than
    silently shipping a map that renders every century at once.
    """
    if BASEMAP != 'ohm':
        return ''
    if not os.path.exists(OHM_DATES_PLUGIN):
        raise FileNotFoundError(
            f"BASEMAP='ohm' needs the date-filter plugin at {OHM_DATES_PLUGIN!r}, which is "
            f"missing. Fetch it from https://github.com/OpenHistoricalMap/maplibre-gl-dates "
            f"(CC0), or set BASEMAP='maptiler'.")
    with open(OHM_DATES_PLUGIN, encoding='utf-8') as f:
        src = f.read()
    # The plugin attaches filterByDate to maplibregl.Map.prototype at load time by
    # checking for `maplibregl` in window, so it must run AFTER maplibre-gl.js.
    return ('<script>/* maplibre-gl-dates (CC0) -- OpenHistoricalMap. '
            'Adds map.filterByDate(). Vendored; see vendor/maplibre-gl-dates.LICENSE */\n'
            + src.replace('</', '<\\/') + '\n</script>')


def _json_safe(obj):
    return json.dumps(obj).replace('</', '<\\/')


def _assert_balanced_divs(name, markup):
    """Guard against unbalanced <div>s. An unclosed div makes the browser silently
    nest whatever follows *inside* it -- which is how #page-data and #page-info once
    ended up trapped inside #page-map, making the Data and Info tabs render blank.
    Syntax checks and getElementById() both miss this, so we check it at build time."""
    opens, closes = markup.count('<div'), markup.count('</div>')
    if opens != closes:
        raise AssertionError(
            f"{name}: unbalanced divs -- {opens} opened, {closes} closed "
            f"({opens - closes:+d}). The page structure would be silently reparented.")


PAGE_IDS = ('page-map', 'page-data', 'page-index', 'page-info')


def _assert_pages_are_siblings(doc):
    """Every .dash-page container must be a sibling under #dash-content, never nested.
    Checked pairwise so each page provably closes before the next one opens."""
    for pid in PAGE_IDS:
        if f'id="{pid}"' not in doc:
            raise AssertionError(f"missing {pid} in output")
    idxs = [doc.index(f'id="{pid}"') for pid in PAGE_IDS]
    if idxs != sorted(idxs):
        raise AssertionError("dash pages are out of order in the document")
    for a, b in zip(PAGE_IDS, PAGE_IDS[1:]):
        between = doc[doc.index(f'id="{a}"'):doc.index(f'id="{b}"')]
        if between.count('<div') != between.count('</div>'):
            raise AssertionError(
                f"#{a} does not close before #{b} -- the later tabs would be nested "
                f"inside it and render blank.")


# ============================ ASSEMBLY ============================
def build_site():
    d = build_data_layer(CSV_PATH)

    # --- data layer artifact: pure data, no presentation. Lets the map page be
    # developed standalone against this file, decoupled from the Python build. ---
    site_payload = {
        'people': d['people'], 'categories': d['categories'], 'categoryMeta': d['categoryMeta'],
        'noteMeta': d['noteMeta'], 'valueLabelMaps': d['valueLabelMaps'],
        'minYear': d['minYear'], 'maxYear': d['maxYear'],
    }
    with open(OUTPUT_DATA, 'w', encoding='utf-8') as f:
        json.dump(site_payload, f)
    print(f"Wrote {OUTPUT_DATA} ({os.path.getsize(OUTPUT_DATA):,} bytes)")

    print("Rendering presentation layer (MapLibre)...")
    _assert_balanced_divs('MAP_MARKUP', MAP_MARKUP)
    _assert_label_font_is_servable()
    map_js = (MAP_JS_A + MAP_JS_B + MAP_JS_C + MAP_JS_D) \
        .replace('__FMT_YEAR__', FMT_YEAR_JS) \
        .replace('__HIDDEN_POPUP_ROWS__', _json_safe(_hidden_popup_labels())) \
        .replace('__SITE__', _json_safe(site_payload)) \
        .replace('__STYLE_URL__', _json_safe(_basemap_style_url())) \
        .replace('__BASEMAP__', _json_safe(BASEMAP)) \
        .replace('__LABEL_FONT__', _json_safe(_basemap_label_font())) \
        .replace('__BASEMAP_DATE_STEP__', _json_safe(BASEMAP_DATE_STEP)) \
        .replace('__LABEL_LANG__', _json_safe(OHM_LABEL_LANGUAGE if BASEMAP == 'ohm' else '')) \
        .replace('__INFRA_PREFIXES__', _json_safe(list(OHM_INFRASTRUCTURE_PREFIXES))) \
        .replace('__BORDER_SOURCE_LAYERS__', _json_safe(list(OHM_BORDER_SOURCE_LAYERS))) \
        .replace('__VIEW_BOUNDS__', _json_safe([list(DEFAULT_VIEW_BOUNDS[0]), list(DEFAULT_VIEW_BOUNDS[1])])) \
        .replace('__FALLBACK_CENTER__', _json_safe(list(FALLBACK_CENTER))) \
        .replace('__FALLBACK_ZOOM__', _json_safe(FALLBACK_ZOOM)) \
        .replace('__FILTER_CODES__', _json_safe(_filter_code_map())) \
        .replace('__ATTRIBUTION__', _json_safe(OHM_ATTRIBUTION if BASEMAP == 'ohm' else ''))

    if DATA_TAB_COMING_SOON:
        page_data = '<div id="page-data" class="dash-page"><div class="dash-blank">Data Tab Coming Soon</div></div>'
    else:
        page_data = _build_data_page(d['data_records'], d['data_columns'],
                                     d['data_filter_meta'], d['data_category_options'],
                                     d['valueLabelMaps'])
    page_index = build_index_page()
    page_info = build_info_page()

    if FEEDBACK_URL:
        feedback = ('<a class="dash-feedback" href="' + html_lib.escape(FEEDBACK_URL) + '" target="_blank" '
                    'rel="noopener noreferrer">Feedback <span aria-hidden="true">&#8599;</span></a>')
    else:
        feedback = ('<a class="dash-feedback" href="#" onclick="alert(\'Feedback link is not set yet.\'); '
                    'return false;">Feedback <span aria-hidden="true">&#8599;</span></a>')

    nav = ('<nav id="dash-nav"><div class="dash-brand">'
           '<span class="dash-brand-title">' + html_lib.escape(MAP_TITLE) +
           '<span class="dash-beta">BETA</span></span>'
           '<span class="dash-brand-sub">' + html_lib.escape(MAP_SUBTITLE) + '</span></div>'
           '<div class="dash-tabs">'
           '<button class="dash-tab dash-tab-active" data-page="page-map" onclick="dashShowPage(\'page-map\', this)">Map</button>'
           '<button class="dash-tab" data-page="page-data" onclick="dashShowPage(\'page-data\', this)">Data</button>'
           '<button class="dash-tab" data-page="page-index" onclick="dashShowPage(\'page-index\', this)">Index</button>'
           '<button class="dash-tab" data-page="page-info" onclick="dashShowPage(\'page-info\', this)">Info</button>'
           '</div>'
           '<div class="dash-actions">'
           '<button id="copy-link" class="dash-copy" type="button" '
           'title="Copy a link that reopens this exact view">Copy link</button>'
           '</div>' + feedback + '</nav>')

    page_title = html_lib.escape(MAP_TITLE + ' \u2014 ' + MAP_SUBTITLE)
    desc = html_lib.escape(PAGE_DESCRIPTION)
    favicon_svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                   '<rect width="32" height="32" rx="6" fill="#1f2d3a"/>'
                   '<line x1="10" y1="22" x2="22" y2="10" stroke="#7f8c8d" stroke-width="2"/>'
                   '<circle cx="10" cy="22" r="5" fill="#2ecc71"/>'
                   '<circle cx="22" cy="10" r="5" fill="#e74c3c"/></svg>')
    favicon = 'data:image/svg+xml;base64,' + base64.b64encode(favicon_svg.encode('utf-8')).decode('ascii')
    og_img = ('<meta property="og:image" content="' + html_lib.escape(OG_IMAGE_URL) + '">'
              '<meta name="twitter:image" content="' + html_lib.escape(OG_IMAGE_URL) + '">') if OG_IMAGE_URL else ''

    head = (
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>' + page_title + '</title>'
        '<meta name="description" content="' + desc + '">'
        '<meta property="og:title" content="' + page_title + '">'
        '<meta property="og:description" content="' + desc + '">'
        '<meta property="og:type" content="website">'
        '<meta property="og:site_name" content="' + html_lib.escape(MAP_TITLE) + '">'
        + og_img +
        '<meta name="twitter:card" content="' + ('summary_large_image' if OG_IMAGE_URL else 'summary') + '">'
        '<meta name="twitter:title" content="' + page_title + '">'
        '<meta name="twitter:description" content="' + desc + '">'
        '<link rel="icon" href="' + favicon + '">'
        '<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">'
        '<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>'
        # must follow maplibre-gl.js: it patches maplibregl.Map.prototype on load
        + _load_dates_plugin() +
        '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>'
        + SHELL_CSS + MAP_CSS
    )

    doc = ('<!DOCTYPE html><html lang="en"><head>' + head + '</head><body>'
           + nav
           + '<div id="dash-content">'
           + MAP_MARKUP + page_data + page_index + page_info
           + '</div>'
           + TAB_SCRIPT
           + map_js
           + '</body></html>')

    _assert_pages_are_siblings(doc)

    with open(OUTPUT_HTML, 'w', encoding='utf-8') as f:
        f.write(doc)
    print(f"Wrote {OUTPUT_HTML} ({os.path.getsize(OUTPUT_HTML):,} bytes)")
    print(f"Success! MapLibre vector dashboard built. Timeline {d['minYear']}-{d['maxYear']}.")
    return doc


if __name__ == "__main__":
    build_site()