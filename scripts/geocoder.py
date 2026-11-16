"""
Multi-source historical/modern geocoder with result aggregation.

Dependencies:
    pip install pandas requests certifi geopy --break-system-packages
    pip install git+https://github.com/isawnyu/pleiades_search_api.git --break-system-packages

    Optional (improves country-name resolution for the structured queries
    described in note 6 below; the script works fine without it thanks to
    the built-in COUNTRY_TO_ISO2 table, but installing it broadens coverage
    beyond that table automatically):
    pip install pycountry --break-system-packages

Notes:

1. Pleiades: `http://api.pleiades.stoa.org/search` is not a real endpoint —
   Pleiades never documented a raw JSON search API at that path, which is why
   it was returning HTML instead of JSON. Pleiades' own supported way to
   search programmatically is the `pleiades_search_api` package, so that's
   used here instead of a hand-built HTTP call. Also note: Pleiades' core
   coverage runs from roughly 1000 BCE to about 640 CE (it's expanding into
   early medieval geography, but that's still in progress) — for anything in
   the 1600-1900 range, Pleiades will essentially always return "Unknown."
   That's expected, not a bug.

2. Getty TGN has been dropped entirely and replaced with the Mapbox
   Geocoding API v6. Getty's SPARQL endpoints turned out to be unusable for
   anonymous public queries during Getty's ongoing infrastructure migration
   (as of mid-2026): the legacy endpoint returned 200 OK with a zero-byte
   body, and the newer endpoint required an auth token we don't have. Rather
   than keep chasing a moving target, Mapbox is a stable, well-documented,
   modern-place geocoder that's a straightforward drop-in for the fourth
   comparison source. You'll need a Mapbox access token — get one free at
   https://console.mapbox.com/account/access-tokens/ (Mapbox's free tier
   includes 100,000 geocoding requests/month as of this writing; verify
   current limits on your account page since pricing/limits can change).

3. MapTiler added as a fifth source, using their Geocoding API
   (GET https://api.maptiler.com/geocoding/{query}.json). You'll need a
   MapTiler API key — get one free at
   https://cloud.maptiler.com/account/keys/. Note: "OpenLayers" was NOT
   added, since it's a browser mapping library, not a geocoding data
   provider — it has no API of its own to query.

4. AGGREGATION: each location is queried against all sources. Results are
   clustered by geographic distance using transitive (union-find) grouping:
   points within CLUSTER_DISTANCE_KM of each other, chained through shared
   neighbors, are treated as "the same place." The largest cluster is
   averaged to produce the aggregate coordinate; sources outside that
   cluster are treated as outliers and flagged in the end-of-run report
   rather than allowed to silently skew the average. If only one source
   returns a result, that result is used as-is. If multiple clusters tie for
   largest, the tie is broken by SOURCE_PRIORITY below. IMPORTANT: if NO two
   sources agree at all (every source lands in its own singleton cluster),
   the aggregate is deliberately left "Unknown" rather than picking one
   source via SOURCE_PRIORITY — with zero corroboration, that tie-break is
   just as likely to select a mismatched result as a correct one (this was
   observed directly: an obscure place name caused every source to disagree,
   and the then-top-priority source had matched an unrelated, wrong entity
   in a different country entirely). These "no consensus" rows are reported
   separately from ordinary partial disagreements, with every source's raw
   value listed, so they can be checked by hand.

5. WIKIDATA added as a sixth source. It's queried via Wikidata's own
   wbsearchentities + wbgetclaims API (no SPARQL needed). This is
   particularly useful for your 1600-1900 records: Wikidata carries a lot of
   historical/renamed place aliases (e.g. Danzig, Christiania, Königsberg)
   that pure modern-place geocoders can miss, and it needs no API key. Since
   Wikidata's own entity search can occasionally match an ambiguous or wrong
   candidate for very common place names, it participates in the same
   distance-clustering aggregation as everything else — if it disagrees with
   the other sources, it gets flagged as an outlier rather than silently
   trusted.

6. WIKIPEDIA added as a seventh source, queried via Wikipedia's own MediaWiki
   API (search + coordinates in a single request, no API key needed). Many
   Wikipedia articles carry a manually-verified {{coord}} value in their
   infobox, and per your own spot-checking these tend to be reliable —
   especially for older towns, where a dedicated local editor has often
   pinned down the historical town center more precisely than a generic
   geocoder would. Like Wikidata, it's just one more vote in the same
   distance-clustering aggregation, so an occasional wrong-article match
   still gets caught as an outlier rather than trusted blindly.

7. STRUCTURED QUERIES: Nominatim, Mapbox, GeoNames, and MapTiler all support
   passing city/region/country as separate fields instead of one free-text
   string, which meaningfully reduces the odds of a wrong-country match
   (structured queries only, not full postal addresses):
     - Nominatim: geopy's structured query dict ({"city":..., "state":...,
       "country":...}), using the country's plain-text name.
     - Mapbox v6: structured input params (place=, region=, country=), where
       `country` must be an ISO 3166-1 alpha-2 code.
     - GeoNames: `country` (ISO 3166-1 alpha-2) and `adminName1` (state/
       region name) filter params alongside the free-text `q`.
     - MapTiler: `country` (ISO 3166-1 alpha-2) acts as a strict filter
       alongside the free-text path segment (MapTiler doesn't support a
       separate city field, only a country fence).
     - Pleiades, Wikidata, and Wikipedia don't offer structured place/country
       search, so they continue to use the free-text query.
   Since Mapbox/GeoNames/MapTiler need an ISO 3166-1 alpha-2 country code,
   COUNTRY_TO_ISO2 below maps your dataset's country values (including
   historical/non-standard ones like "Prussia", "Scotland", or
   "Austria-Hungary") to a best-effort modern code. If a country doesn't
   resolve to a code (e.g. the genuinely ambiguous "North Africa"), or if
   the structured query returns nothing, each function automatically falls
   back to the original free-text query rather than giving up.
"""

import ssl
import sys
import os
import shutil
import certifi
import pandas as pd
import requests
import time
from math import radians, sin, cos, sqrt, atan2
from urllib.parse import quote
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter

try:
    from pleiades_search_api.search import Query as PleiadesQuery, SearchInterface as PleiadesSearchInterface
    _PLEIADES_AVAILABLE = True
except ImportError:
    _PLEIADES_AVAILABLE = False

def _import_paths():
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

# Reviewed extract for this volume. Change VOLUME to geocode a different one.
VOLUME = "02"  # e.g. "01", "02", "15" -- drives the archive filenames below
ARCHIVE_DIR = paths.ARCHIVE_DIR
INPUT_CSV = os.path.join(ARCHIVE_DIR, f"dsb_extracted_reviewed_vol{VOLUME}.csv")
OUTPUT_CSV = os.path.join(paths.ROOT, "dsb_geocoded.csv")  # working copy; the archive copy is separate
REPORT_TXT = os.path.join(ARCHIVE_DIR, f"dsb_geocode_vol{VOLUME}.txt")
ARCHIVE_CSV = os.path.join(ARCHIVE_DIR, f"dsb_geocoded_vol{VOLUME}.csv")

# --- CREDENTIALS ---
GEONAMES_USERNAME = "seancoath"  # Your active GeoNames username
MAPBOX_ACCESS_TOKEN = "pk.eyJ1Ijoic2VhbmNvYXRoIiwiYSI6ImNtcWZxMGszdTFkemUycHB3NWYzdGoyeTcifQ.HEJDWO48tRcdjhkN2D60hg"  # https://console.mapbox.com/account/access-tokens/
MAPTILER_API_KEY = "WS5k0XeBJBUUDowtC8l1"  # https://cloud.maptiler.com/account/keys/
USER_AGENT = "combined_geocoder/1.0 (+mailto:s@gmail.com)"  # set a real contact per Pleiades/Nominatim/Wikidata etiquette

# --- AGGREGATION SETTINGS ---
CLUSTER_DISTANCE_KM = 40  # sources within this distance of each other count as "agreeing"
# Dedicated place geocoders first; Wikipedia/Wikidata (entity search, which can
# occasionally latch onto a wrong same-named entity with no other corroboration)
# ranked below them, and only used to break ties when 2+ dedicated geocoders
# already agree with each other. Pleiades last since it's rarely applicable
# outside antiquity. See aggregate_coords() for how ties are actually used --
# a total-disagreement case (no cluster of size >1 at all) is NOT resolved by
# this list; it's flagged as "no consensus" instead of guessing.
SOURCE_PRIORITY = ["nominatim", "geonames", "mapbox", "maptiler", "wikipedia", "wikidata", "pleiades"]

# --- PLEIADES YEAR CUTOFF ---
# Pleiades' core coverage runs from roughly 1000 BCE to about 640 CE. Querying
# it for a clearly post-classical location (a) wastes an API call and (b)
# risks a spurious namesake match getting flagged as a "disagreement" purely
# because it's the wrong historical place with a similar name (e.g. searching
# "Vienna, Austria" can surface the ancient Gallo-Roman city Vienna in
# France). If a location has a known year attached and it's after this
# cutoff, Pleiades is skipped for that lookup. Locations with no year
# information still query Pleiades, since we can't rule out an ancient date.
PLEIADES_MAX_YEAR = 700

# --- HISTORICAL PLACE NAME / COUNTRY OVERRIDES ---
# Cities that have been renamed and/or changed countries since the source
# data's "country" label was written (e.g. "Danzig, Germany" — Danzig has
# been Gdańsk, Poland since 1945). This matters a lot here: the structured
# queries in note 7 use the dataset's country as a strict filter for
# Mapbox/GeoNames/MapTiler, so an outdated country actively steers those
# sources to search in the WRONG modern country and produces confident-looking
# but wrong (or mutually disagreeing) matches — this is NOT caught by the
# "structured search found nothing, fall back to free text" logic, since the
# structured search still finds *something*, just in the wrong country.
# This table rewrites the city/country pair to its modern equivalent before
# any query (structured or free-text) is built. Keys are matched
# case-insensitively against the parsed city name. Add more entries as you
# spot them in your disagreement report — a location whose sources disagree
# with each other (not just "outlier: pleiades") is the signal to check here.
HISTORICAL_PLACE_OVERRIDES = {
    "danzig": ("Gdańsk", "Poland"),
    "königsberg": ("Kaliningrad", "Russia"),
    "koenigsberg": ("Kaliningrad", "Russia"),
    "breslau": ("Wrocław", "Poland"),
    "elbing": ("Elbląg", "Poland"),
    "stettin": ("Szczecin", "Poland"),
    "posen": ("Poznań", "Poland"),
    "thorn": ("Toruń", "Poland"),
    "lemberg": ("Lviv", "Ukraine"),
    "pressburg": ("Bratislava", "Slovakia"),
    "karlsbad": ("Karlovy Vary", "Czech Republic"),
    "reval": ("Tallinn", "Estonia"),
    "dorpat": ("Tartu", "Estonia"),
    "constantinople": ("Istanbul", "Turkey"),
    "bombay": ("Mumbai", "India"),
    "batavia": ("Jakarta", "Indonesia"),
    "saigon": ("Ho Chi Minh City", "Vietnam"),
    "peking": ("Beijing", "China"),
    "canton": ("Guangzhou", "China"),
}


def resolve_historical_place(city, country):
    """
    If `city` matches a known renamed/relocated historical place, return its
    modern (city, country) equivalent. Otherwise return (city, country)
    unchanged. Matching is case-insensitive on the city name only.
    """
    if not city:
        return city, country
    key = city.strip().lower()
    if key in HISTORICAL_PLACE_OVERRIDES:
        modern_city, modern_country = HISTORICAL_PLACE_OVERRIDES[key]
        return modern_city, modern_country
    return city, country


def parse_year(value):
    """Parse a year field into an int, or None if missing/unparseable."""
    if is_unknown(value):
        return None
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None


def split_city_state(value):
    """
    Splits a Born_Town/Died_Town value that may now carry an embedded
    state/region, e.g. "Baltimore, Maryland" -> ("Baltimore", "Maryland").
    If there's no comma (e.g. "Paris"), or the part after the comma is
    itself "Unknown"/blank, state is None.
    """
    if not isinstance(value, str):
        return value, None
    parts = [p.strip() for p in value.split(",")]
    city = parts[0]
    if len(parts) == 1:
        return city, None
    region = ", ".join(parts[1:]).strip()
    return city, (None if is_unknown(region) else region)


_COUNTRY_SAME = {
    "united states": "united states", "usa": "united states", "us": "united states",
    "united states of america": "united states",
    "united kingdom": "united kingdom", "uk": "united kingdom", "great britain": "united kingdom",
    "england": "united kingdom", "scotland": "united kingdom", "wales": "united kingdom",
    "russia": "russia", "russian federation": "russia",
    "czech republic": "czechia", "czechia": "czechia",
}


def countries_conflict(stored, found):
    """True when a geocoded point lies in a different country from the one stored.

    England and United Kingdom are the same place for this check. A missing
    name on either side is not a conflict.
    """
    def norm(value):
        text = " ".join(str(value or "").casefold().replace(".", " ").split())
        return _COUNTRY_SAME.get(text, text)
    a, b = norm(stored), norm(found)
    if not a or not b or a in {"unknown", "none"} or b in {"unknown", "none"}:
        return False
    return a != b


def build_free_text_query(city, state, country):
    """Builds a "City, State, Country" (or subset) free-text query string."""
    bits = [b for b in [state, country] if b]
    return f"{city}, {', '.join(bits)}" if bits else city

# --- COUNTRY NAME -> ISO 3166-1 ALPHA-2 (for structured queries to Mapbox/GeoNames/MapTiler) ---
# Covers every value seen in this dataset's Born_Country/Died_Country/Education_Locations
# fields, plus common variants. Historical/non-standard entries map to a best-effort modern
# equivalent (documented inline). Add more entries here as needed.
COUNTRY_TO_ISO2 = {
    "Argentina": "AR", "Austria": "AT", "Belgium": "BE", "Canada": "CA",
    "Denmark": "DK", "Egypt": "EG", "Estonia": "EE", "France": "FR",
    "Germany": "DE", "Greece": "GR", "India": "IN", "Iraq": "IQ",
    "Ireland": "IE", "Italy": "IT", "Japan": "JP", "Latvia": "LV",
    "Mexico": "MX", "Morocco": "MA", "Netherlands": "NL", "Norway": "NO",
    "Poland": "PL", "Portugal": "PT", "Russia": "RU", "Spain": "ES",
    "Sweden": "SE", "Switzerland": "CH", "Syria": "SY", "United Kingdom": "GB",
    "United States": "US", "Haiti": "HT", "Hungary": "HU", "Iran": "IR",
    "Peru": "PE", "Turkey": "TR", "the Netherlands": "NL", "Cape Verde Islands": "CV",
    # UK constituent countries -> GB (Nominatim structured search handles
    # these natively via plain text; this table is only for the sources
    # that require ISO codes)
    "England": "GB", "Scotland": "GB", "Wales": "GB", "Northern Ireland": "GB",
    # US states occasionally appearing in the "country" field position
    "Vermont": "US", "Connecticut": "US", "Virginia": "US",
    # German states/regions
    "Bavaria": "DE",
    # Historical political entities -> best-effort modern equivalent
    "Austria-Hungary": "AT",
    "Prussia": "DE",
    "Republic of Dubrovnik": "HR",
    "Czechoslovakia": "CZ",
    "German Federal Republic": "DE",  # historical name for West Germany
    "Principality of Württemberg–Montbéliard": "DE",
    "U.S.S.R.": "RU",
    "Union of Soviet Socialist Republics": "RU",
    "Ukrainian S.S.R.": "UA",
    "Transylvania": "RO",  # historical region, now part of Romania
    "Magna Graecia": "IT",  # historical region, ancient Greek colonies in southern Italy
    # Too vague/broad to safely filter by (span multiple modern countries) -> left unmapped on purpose
    "North Africa": None,
    "Asia Minor": None,
    "Cilicia": None,
    "Khurasan": None,
    "Roman Empire": None,
    "Unknown": None,
}


def get_iso2(country_name):
    """Resolve a country name to an ISO 3166-1 alpha-2 code, or None if unresolvable."""
    if not country_name or not isinstance(country_name, str):
        return None
    name = country_name.strip()
    if name in COUNTRY_TO_ISO2:
        return COUNTRY_TO_ISO2[name]
    for key, val in COUNTRY_TO_ISO2.items():
        if key.lower() == name.lower():
            return val
    # Optional: broaden coverage automatically if pycountry is installed
    try:
        import pycountry
        match = pycountry.countries.search_fuzzy(name)
        if match:
            return match[0].alpha_2
    except Exception:
        pass
    return None


def _main():
    if not os.path.exists(INPUT_CSV):
        sys.exit(
            f"Input CSV not found: {INPUT_CSV}\n"
            "Set VOLUME in scripts/geocoder.py to a volume that has "
            "archive/dsb_extracted_reviewed_volXX.csv."
        )
    if os.path.exists(ARCHIVE_CSV) and "--overwrite" not in sys.argv:
        sys.exit(
            f"{ARCHIVE_CSV} already exists. Set VOLUME to a volume that has not been "
            "geocoded, or pass --overwrite to replace this volume's archived geocode."
        )
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    # --- 0. Mirror all console output to a report file, line-by-line ---------
    # Every print() from here on (per-row progress lines AND the final
    # failed/no-consensus/disagreement report at the end) is written to both the
    # console and REPORT_TXT, in the same order, as it happens.
    class _Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, data):
            for s in self.streams:
                s.write(data)

        def flush(self):
            for s in self.streams:
                s.flush()


    _report_file = open(REPORT_TXT, "w", encoding="utf-8", buffering=1)  # line-buffered: survives a mid-run crash
    sys.stdout = _Tee(sys.__stdout__, _report_file)

    # --- 1. SSL context using certifi (fixes many macOS SSL issues) ----------
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    # --- 2. Load data --------------------------------------------------------
    df = pd.read_csv(INPUT_CSV)


    def is_unknown(value) -> bool:
        return not isinstance(value, str) or value.strip().lower() == "unknown" or value.strip() == ""


    # --- 3. Init Geocoders & Cache ---------------------------------------
    geolocator = Nominatim(
        user_agent=USER_AGENT,
        ssl_context=ssl_ctx,
    )
    # Use geopy's RateLimiter to safely query Nominatim
    nominatim_geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1)

    if _PLEIADES_AVAILABLE:
        pleiades_interface = PleiadesSearchInterface(user_agent=USER_AGENT)

    _cache = {}
    _country_cache = {}

    def reverse_country(lat, lon):
        """The modern country of a coordinate, or None if the lookup fails."""
        key = (round(float(lat), 2), round(float(lon), 2))
        if key in _country_cache:
            return _country_cache[key]
        found = None
        try:
            loc = geolocator.reverse((key[0], key[1]), language="en", timeout=10, zoom=5)
            if loc is not None:
                found = (loc.raw.get("address") or {}).get("country")
        except Exception:
            found = None
        _country_cache[key] = found
        return found

    def apply_country_check(agg_lat, agg_lon, stored_country):
        if agg_lat == "Unknown" or is_unknown(stored_country):
            return agg_lat, agg_lon, None
        found = reverse_country(agg_lat, agg_lon)
        if found and countries_conflict(stored_country, found):
            return ("Unknown", "Unknown",
                    f"COUNTRY: cluster is in {found}; stored country is {stored_country}")
        return agg_lat, agg_lon, None

    failed_lookups = []     # (row_num, field, query, reason) - ALL sources returned Unknown
    disagreements = []      # (row_num, field, query, note)   - sources disagreed, but a majority cluster still won
    no_consensus = []       # (row_num, field, query, note)   - NO two sources agreed at all; aggregate left "Unknown"


    def clean_place_for_historical_db(place_name):
        """Extracts the town/city name (text before the first comma) for historical DBs."""
        if "," in place_name:
            return place_name.split(",")[0].strip()
        return place_name.strip()


    # --- GEOCODER FUNCTIONS ---
    # Each returns (result, error). result is "lat, lon" or "Unknown".
    # Functions that support structured input try that first, then fall back
    # to the free-text query if the structured attempt returns nothing.

    def get_pleiades_coords(place_name):
        """Query Pleiades via the official pleiades_search_api package for ancient coordinates."""
        if not _PLEIADES_AVAILABLE:
            return "Unknown", "pleiades_search_api not installed"

        search_term = clean_place_for_historical_db(place_name)
        try:
            q = PleiadesQuery()
            q.set_parameter("text", search_term)
            results = pleiades_interface.search(q)
            hits = results.get("hits", [])
            if not hits:
                return "Unknown", f"No result found for '{search_term}'"

            # Pull coordinates from the first hit's full place JSON
            place_uri = hits[0]["uri"].rstrip("/") + "/json"
            resp = requests.get(place_uri, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                repr_point = data.get("reprPoint")
                if repr_point and len(repr_point) == 2:
                    lon, lat = repr_point
                    return f"{lat}, {lon}", None
            return "Unknown", "No reprPoint found on matched place"
        except Exception as e:
            return "Unknown", f"Pleiades request error: {e}"


    def _mapbox_request(params):
        url = "https://api.mapbox.com/search/geocode/v6/forward"
        try:
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 401:
                return "Unknown", "Mapbox 401 - check MAPBOX_ACCESS_TOKEN"
            if response.status_code == 429:
                return "Unknown", "Mapbox 429 - rate limit exceeded"
            if response.status_code != 200:
                return "Unknown", f"Mapbox HTTP {response.status_code}"

            try:
                data = response.json()
            except ValueError:
                return "Unknown", "Mapbox returned non-JSON payload"

            features = data.get("features", [])
            if not features:
                return "Unknown", "No result found"

            lon, lat = features[0]["geometry"]["coordinates"]
            return f"{lat}, {lon}", None
        except Exception as e:
            return "Unknown", f"Mapbox request error: {e}"


    def get_mapbox_coords(city, state, iso2, fallback_query):
        """
        Query the Mapbox Geocoding API v6 (forward geocoding) for modern coordinates.
        Uses structured input (place/region/country) when a city + ISO2 country code
        are available; falls back to a free-text query otherwise or if the structured
        attempt returns nothing.

        Docs: https://docs.mapbox.com/api/search/geocoding/#forward-geocoding-with-structured-input
        """
        if not MAPBOX_ACCESS_TOKEN or MAPBOX_ACCESS_TOKEN == "YOUR_MAPBOX_ACCESS_TOKEN":
            return "Unknown", "MAPBOX_ACCESS_TOKEN not set"

        if city and iso2:
            params = {"place": city, "country": iso2, "limit": 1, "access_token": MAPBOX_ACCESS_TOKEN}
            if state:
                params["region"] = state
            result, err = _mapbox_request(params)
            if result != "Unknown":
                return result, err

        # Free-text fallback (no structured components available, or structured attempt found nothing)
        params = {"q": fallback_query, "limit": 1, "access_token": MAPBOX_ACCESS_TOKEN}
        return _mapbox_request(params)


    def _maptiler_request(query_text, params):
        url = f"https://api.maptiler.com/geocoding/{quote(query_text)}.json"
        try:
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 403:
                return "Unknown", "MapTiler 403 - check MAPTILER_API_KEY"
            if response.status_code != 200:
                return "Unknown", f"MapTiler HTTP {response.status_code}"

            try:
                data = response.json()
            except ValueError:
                return "Unknown", "MapTiler returned non-JSON payload"

            features = data.get("features", [])
            if not features:
                return "Unknown", "No result found"

            center = features[0].get("center")
            if not center or len(center) != 2:
                return "Unknown", "Matched feature had no center coordinates"

            lon, lat = center
            return f"{lat}, {lon}", None
        except Exception as e:
            return "Unknown", f"MapTiler request error: {e}"


    def get_maptiler_coords(city, iso2, fallback_query):
        """
        Query the MapTiler Geocoding API (forward geocoding) for modern coordinates.
        MapTiler doesn't offer a separate city field, but its `country` param acts as
        a strict filter, so it's used as a semi-structured query when a country code
        is available. Falls back to an unfiltered free-text query if that returns
        nothing (the country fence can otherwise exclude a valid but mismapped entry).

        Docs: https://docs.maptiler.com/cloud/api/geocoding/
        """
        if not MAPTILER_API_KEY or MAPTILER_API_KEY == "YOUR_MAPTILER_API_KEY":
            return "Unknown", "MAPTILER_API_KEY not set"

        query_text = city if city else fallback_query

        if iso2:
            params = {"key": MAPTILER_API_KEY, "limit": 1, "country": iso2}
            result, err = _maptiler_request(query_text, params)
            if result != "Unknown":
                return result, err

        params = {"key": MAPTILER_API_KEY, "limit": 1}
        return _maptiler_request(fallback_query, params)


    def _geonames_request(params):
        url = "http://api.geonames.org/searchJSON"
        try:
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    return "Unknown", "GeoNames returned non-JSON payload"

                if 'status' in data:
                    return "Unknown", f"GeoNames API Error: {data['status'].get('message')}"
                if 'geonames' in data and len(data['geonames']) > 0:
                    res = data['geonames'][0]
                    return f"{res['lat']}, {res['lng']}", None
            return "Unknown", "No result found"
        except Exception as e:
            return "Unknown", f"GeoNames request error: {e}"


    def get_geonames_coords(query_text, iso2, state):
        """
        Query GeoNames for standard coordinates. Adds `country` (ISO2) and
        `adminName1` (state/region) as filters alongside the free-text `q` when
        available; falls back to an unfiltered query if the filtered attempt
        returns nothing.
        """
        base_params = {'q': query_text, 'maxRows': 1, 'username': GEONAMES_USERNAME}

        if iso2:
            params = dict(base_params)
            params['country'] = iso2
            if state:
                params['adminName1'] = state
            result, err = _geonames_request(params)
            if result != "Unknown":
                return result, err

        return _geonames_request(base_params)


    def get_nominatim_coords(city, state, country, fallback_query):
        """
        Query Nominatim (OSM) for modern coordinates. Uses geopy's structured
        query (city/state/country as separate fields) when a city is available;
        falls back to a free-text query otherwise or if the structured attempt
        returns nothing.
        """
        if city:
            query_dict = {"city": city}
            if state:
                query_dict["state"] = state
            if country:
                query_dict["country"] = country
            try:
                loc = nominatim_geocode(query_dict, addressdetails=False, language="en", timeout=10)
            except Exception as e:
                return "Unknown", f"Nominatim error (structured): {e}"
            if loc:
                return f"{loc.latitude}, {loc.longitude}", None

        try:
            loc = nominatim_geocode(fallback_query, addressdetails=False, language="en", timeout=10)
        except Exception as e:
            return "Unknown", f"Nominatim error: {e}"
        if loc:
            return f"{loc.latitude}, {loc.longitude}", None
        return "Unknown", "No result found"


    def get_wikidata_coords(place_name):
        """
        Query Wikidata via its own search + claims API (no SPARQL needed) for a
        coordinate location (property P625). Especially useful for 1600-1900
        places that have since been renamed, since Wikidata tracks historical
        aliases that pure modern-place geocoders often don't.
        """
        try:
            search_resp = requests.get(
                "https://www.wikidata.org/w/api.php",
                params={
                    "action": "wbsearchentities",
                    "search": place_name,
                    "language": "en",
                    "type": "item",
                    "format": "json",
                    "limit": 5,
                },
                headers={"User-Agent": USER_AGENT},
                timeout=10,
            )
            search_data = search_resp.json()
            candidates = search_data.get("search", [])
            if not candidates:
                return "Unknown", f"No Wikidata entity found for '{place_name}'"

            qid = candidates[0]["id"]

            claims_resp = requests.get(
                "https://www.wikidata.org/w/api.php",
                params={
                    "action": "wbgetclaims",
                    "entity": qid,
                    "property": "P625",  # coordinate location
                    "format": "json",
                },
                headers={"User-Agent": USER_AGENT},
                timeout=10,
            )
            claims_data = claims_resp.json()
            claims = claims_data.get("claims", {}).get("P625", [])
            if not claims:
                return "Unknown", f"No coordinate data on Wikidata entity {qid}"

            value = claims[0]["mainsnak"]["datavalue"]["value"]
            return f"{value['latitude']}, {value['longitude']}", None
        except Exception as e:
            return "Unknown", f"Wikidata request error: {e}"


    def get_wikipedia_coords(place_name):
        """
        Query Wikipedia's MediaWiki API for the coordinates listed on the best-
        matching article (typically the infobox {{coord}} value). Uses a single
        generator=search request that finds the top matching page and returns its
        coordinates in one round trip.
        """
        try:
            resp = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "generator": "search",
                    "gsrsearch": place_name,
                    "gsrlimit": 1,
                    "prop": "coordinates",
                    "format": "json",
                },
                headers={"User-Agent": USER_AGENT},
                timeout=10,
            )
            data = resp.json()
            pages = data.get("query", {}).get("pages", {})
            if not pages:
                return "Unknown", f"No Wikipedia article found for '{place_name}'"

            page = next(iter(pages.values()))
            coords = page.get("coordinates")
            if not coords:
                return "Unknown", f"Wikipedia article '{page.get('title')}' has no coordinates listed"

            lat = coords[0]["lat"]
            lon = coords[0]["lon"]
            return f"{lat}, {lon}", None
        except Exception as e:
            return "Unknown", f"Wikipedia request error: {e}"


    # --- AGGREGATION ---

    def haversine_km(lat1, lon1, lat2, lon2):
        """Great-circle distance between two points, in kilometers."""
        R = 6371.0
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        return R * c


    def aggregate_coords(results):
        """
        Combine the per-source results into a single best coordinate.

        Returns (agg_lat, agg_lon, note, no_consensus).
        - agg_lat/agg_lon are "Unknown" strings if no source returned a result,
          OR if sources were in total disagreement (see no_consensus below).
        - note is None unless sources disagreed (some fell outside the main
          cluster) or there was no consensus at all.
        - no_consensus is True unless a strict majority of the returned
          points lie in one cluster within CLUSTER_DISTANCE_KM. A single
          source is not a majority. The aggregate is left "Unknown" and the
          row is flagged for manual review.
        """
        points = {}
        for src, val in results.items():
            if val != "Unknown":
                try:
                    lat_str, lon_str = val.split(", ")
                    points[src] = (float(lat_str), float(lon_str))
                except Exception:
                    continue

        if not points:
            return "Unknown", "Unknown", None, False

        if len(points) == 1:
            src, (lat, lon) = next(iter(points.items()))
            note = (f"SINGLE SOURCE: only {src} returned ({lat}, {lon}); "
                    "left Unknown until a reviewer accepts it")
            return "Unknown", "Unknown", note, True

        # Union-Find (disjoint set) clustering: two sources merge into the same
        # cluster if they're within CLUSTER_DISTANCE_KM of EITHER a common
        # neighbor or each other, transitively -- this is order-independent and
        # correctly chains e.g. A-B close, B-C close (even if A-C alone would be
        # just over the threshold), unlike a naive "compare only to the cluster's
        # first point" approach.
        names = list(points.keys())
        parent = {n: n for n in names}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                if haversine_km(*points[a], *points[b]) <= CLUSTER_DISTANCE_KM:
                    union(a, b)

        grouped = {}
        for n in names:
            grouped.setdefault(find(n), []).append(n)
        clusters = list(grouped.values())

        best_size = max(len(c) for c in clusters)

        if best_size < 2 or best_size * 2 <= len(points):
            # A place is kept only when a strict majority of the sources that
            # returned a point lie within CLUSTER_DISTANCE_KM of each other.
            # Anything weaker is left Unknown for a reviewer.
            raw_values = ", ".join(f"{src}=({points[src][0]}, {points[src][1]})" for src in names)
            note = (f"NO CONSENSUS: largest cluster is {best_size}/{len(points)} "
                    f"(need a strict majority within {CLUSTER_DISTANCE_KM}km); raw values: {raw_values}")
            return "Unknown", "Unknown", note, True

        # Largest cluster wins; ties broken by which cluster contains the
        # highest-priority source.
        def cluster_sort_key(cluster_srcs):
            best_rank = min(
                (SOURCE_PRIORITY.index(s) for s in cluster_srcs if s in SOURCE_PRIORITY),
                default=len(SOURCE_PRIORITY),
            )
            return (-len(cluster_srcs), best_rank)

        clusters.sort(key=cluster_sort_key)
        best = clusters[0]

        avg_lat = round(sum(points[s][0] for s in best) / len(best), 6)
        avg_lon = round(sum(points[s][1] for s in best) / len(best), 6)

        note = None
        if len(best) < len(points):
            agreeing = ", ".join(best)
            outlier_srcs = [s for s in names if s not in best]
            note = f"{len(best)}/{len(points)} sources agreed ({agreeing}); outlier(s): {', '.join(outlier_srcs)}"

        return avg_lat, avg_lon, note, False




    # --- MAIN MULTI-QUERY HANDLER ---

    def geocode_place_multi(query, row_num, field, city=None, state=None, country=None, skip_pleiades=False):
        """
        Query all sources, printing real-time Success/Fail outcomes. Tracks
        failures/disagreements per row. city/state/country (when known) are used
        for structured queries to the sources that support them; `query` is
        always used as the free-text fallback and for sources without structured
        support (Pleiades, Wikidata, Wikipedia). skip_pleiades bypasses the
        Pleiades call entirely (see PLEIADES_MAX_YEAR above).
        """
        if is_unknown(query):
            return {
                "pleiades": "Unknown", "mapbox": "Unknown", "maptiler": "Unknown",
                "geonames": "Unknown", "nominatim": "Unknown", "wikidata": "Unknown",
                "wikipedia": "Unknown"
            }

        query_clean = query.strip()
        # skip_pleiades is part of the cache key so the same place name doesn't
        # silently reuse a Pleiades-skipped result for a row where it shouldn't be skipped
        cache_key = f"{query_clean.lower()}|skip_pleiades={skip_pleiades}"

        if cache_key in _cache:
            results = _cache[cache_key]
        else:
            iso2 = get_iso2(country)

            if skip_pleiades:
                p_res, p_err = "Unknown", "Skipped (year outside Pleiades' ancient-world coverage)"
            else:
                p_res, p_err = get_pleiades_coords(query_clean)
            m_res, m_err = get_mapbox_coords(city, state, iso2, query_clean)
            mt_res, mt_err = get_maptiler_coords(city, iso2, query_clean)
            g_res, g_err = get_geonames_coords(query_clean, iso2, state)
            n_res, n_err = get_nominatim_coords(city, state, country, query_clean)
            w_res, w_err = get_wikidata_coords(query_clean)
            wp_res, wp_err = get_wikipedia_coords(query_clean)

            p_status = "SKIPPED" if skip_pleiades else ("SUCCESS" if p_res != "Unknown" else "FAIL")
            m_status = "SUCCESS" if m_res != "Unknown" else "FAIL"
            mt_status = "SUCCESS" if mt_res != "Unknown" else "FAIL"
            g_status = "SUCCESS" if g_res != "Unknown" else "FAIL"
            n_status = "SUCCESS" if n_res != "Unknown" else "FAIL"
            w_status = "SUCCESS" if w_res != "Unknown" else "FAIL"
            wp_status = "SUCCESS" if wp_res != "Unknown" else "FAIL"

            print(
                f" Row {row_num}: '{query_clean}' | "
                f"Pleiades: {p_status} | "
                f"Mapbox: {m_status} | "
                f"MapTiler: {mt_status} | "
                f"GeoNames: {g_status} | "
                f"Nominatim: {n_status} | "
                f"Wikidata: {w_status} | "
                f"Wikipedia: {wp_status}"
            )

            results = {
                "pleiades": p_res,
                "mapbox": m_res,
                "maptiler": mt_res,
                "geonames": g_res,
                "nominatim": n_res,
                "wikidata": w_res,
                "wikipedia": wp_res,
            }

            _cache[cache_key] = results
            time.sleep(1.2)  # Throttle to respect API limits

        # Track per-row diagnostics regardless of whether this came from cache,
        # so a place referenced by many rows gets reported for every row it
        # affects, not just the first time it was looked up.
        if all(v == "Unknown" for v in results.values()):
            failed_lookups.append((row_num, field, query_clean, "All sources returned Unknown"))
        else:
            _, _, note, is_no_consensus = aggregate_coords(results)
            if is_no_consensus:
                no_consensus.append((row_num, field, query_clean, note))
            elif note:
                disagreements.append((row_num, field, query_clean, note))

        return results


    def sheet_row(i):
        return i + 2


    # --- 4a. Geocode Born / Died -----------------------------------------------
    # Born_Town/Died_Town may now carry an embedded state/region, e.g.
    # "Baltimore, Maryland" instead of just "Baltimore" -- split_city_state()
    # (defined above) handles both that and the plain "Paris" (no region) case.

    for mode in ["Born", "Died"]:
        p_lats, p_lons = [], []
        m_lats, m_lons = [], []
        mt_lats, mt_lons = [], []
        g_lats, g_lons = [], []
        n_lats, n_lons = [], []
        w_lats, w_lons = [], []
        wp_lats, wp_lons = [], []
        agg_lats, agg_lons = [], []

        print(f"Starting geocoding for {mode} locations...")
        for i, row in df.iterrows():
            country = row.get(f"{mode}_Country")
            town = row.get(f"{mode}_Town")

            if is_unknown(town):
                for lst in [p_lats, p_lons, m_lats, m_lons, mt_lats, mt_lons, g_lats, g_lons,
                            n_lats, n_lons, w_lats, w_lons, wp_lats, wp_lons, agg_lats, agg_lons]:
                    lst.append("Unknown")
                continue

            town_str = str(town).strip()
            country_str = None if is_unknown(country) else str(country).strip()

            # Split any embedded state/region out of the town field
            parsed_city, parsed_state = split_city_state(town_str)

            # Correct known renamed/relocated historical places before building any query
            resolved_city, resolved_country = resolve_historical_place(parsed_city, country_str)
            overridden = (resolved_city != parsed_city or resolved_country != country_str)
            # A historical override replaces the place's identity entirely, so a
            # region tied to the OLD name/country no longer applies
            resolved_state = None if overridden else parsed_state

            query = build_free_text_query(resolved_city, resolved_state, resolved_country)

            year = parse_year(row.get(f"{mode}_Year"))
            skip_pleiades = year is not None and year > PLEIADES_MAX_YEAR

            results = geocode_place_multi(query, sheet_row(i), f"{mode}_Town",
                                           city=resolved_city, state=resolved_state, country=resolved_country,
                                           skip_pleiades=skip_pleiades)


            for prefix, lat_lst, lon_lst in [
                ("pleiades", p_lats, p_lons),
                ("mapbox", m_lats, m_lons),
                ("maptiler", mt_lats, mt_lons),
                ("geonames", g_lats, g_lons),
                ("nominatim", n_lats, n_lons),
                ("wikidata", w_lats, w_lons),
                ("wikipedia", wp_lats, wp_lons),
            ]:
                val = results[prefix]
                if val != "Unknown":
                    lat, lon = val.split(", ")
                    lat_lst.append(lat)
                    lon_lst.append(lon)
                else:
                    lat_lst.append("Unknown")
                    lon_lst.append("Unknown")

            agg_lat, agg_lon, _, _ = aggregate_coords(results)
            agg_lat, agg_lon, country_note = apply_country_check(agg_lat, agg_lon, country_str)
            if country_note:
                no_consensus.append((sheet_row(i), f"{mode}_Town", query, country_note))
                print(f"  {country_note}")
            agg_lats.append(agg_lat)
            agg_lons.append(agg_lon)

        df[f"{mode}_Pleiades_Lat"] = p_lats
        df[f"{mode}_Pleiades_Lon"] = p_lons
        df[f"{mode}_Mapbox_Lat"] = m_lats
        df[f"{mode}_Mapbox_Lon"] = m_lons
        df[f"{mode}_MapTiler_Lat"] = mt_lats
        df[f"{mode}_MapTiler_Lon"] = mt_lons
        df[f"{mode}_GeoNames_Lat"] = g_lats
        df[f"{mode}_GeoNames_Lon"] = g_lons
        df[f"{mode}_Nominatim_Lat"] = n_lats
        df[f"{mode}_Nominatim_Lon"] = n_lons
        df[f"{mode}_Wikidata_Lat"] = w_lats
        df[f"{mode}_Wikidata_Lon"] = w_lons
        df[f"{mode}_Wikipedia_Lat"] = wp_lats
        df[f"{mode}_Wikipedia_Lon"] = wp_lons
        df[f"{mode}_Aggregate_Latitude"] = agg_lats
        df[f"{mode}_Aggregate_Longitude"] = agg_lons

        print(f"Finished geocoding {mode} locations.\n")


    # --- 4b. Geocode Education_Locations ---------------------------------------
    def parse_education_locations(value):
        """
        Parses a string like:
          "CITY, REGION, COUNTRY, START_YEAR, END_YEAR; CITY, REGION, COUNTRY, START_YEAR, END_YEAR"
        into a list of dicts: {"city":..., "state":..., "country":..., "query":...,
        "skip_pleiades":...}.
        REGION is always present as a field now -- literally "Unknown" when not
        specified, same convention as every other field -- so a standard chunk
        always has exactly 5 comma-separated parts. A handful of older/legacy
        chunk shapes (no region field at all, e.g. "CITY, COUNTRY, YEAR, YEAR")
        are still tolerated via the same flexible fallback used previously, in
        case any rows haven't been migrated to the 5-field format. Known
        renamed/relocated historical places (see HISTORICAL_PLACE_OVERRIDES) are
        corrected to their modern city/country before the query is built.
        """
        if is_unknown(value):
            return []

        entries = []
        for chunk in value.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = [p.strip() for p in chunk.split(",")]

            if len(parts) == 5:
                # City, Region, Country, Start_Year, End_Year
                city, region, country = parts[0], parts[1], parts[2]
                years = parts[3:5]
            elif len(parts) == 6:
                # City, Region, Country, Start_Year, End_Year, Degree
                city, region, country = parts[0], parts[1], parts[2]
                years = parts[3:5]
            elif len(parts) > 6:
                # A comma inside the region, plus the degree slot at the end.
                city = parts[0]
                years = parts[-3:-1]
                middle = parts[1:-3]
                if len(middle) >= 2:
                    region, country = ", ".join(middle[:-1]) or None, middle[-1]
                elif len(middle) == 1:
                    region, country = None, middle[0]
                else:
                    region, country = None, None
            elif len(parts) == 4:
                # Legacy format with no region field: City, Country, Year, Year
                city, region, country = parts[0], None, parts[1]
                years = parts[2:4]
            elif len(parts) == 3:
                city, region, country = parts[0], None, parts[1]
                years = [parts[2]]
            elif len(parts) == 2:
                city, region, country = parts[0], None, parts[1]
                years = []
            else:
                city, region, country = parts[0], None, None
                years = []

            if is_unknown(city):
                # Keep a slot so coordinates stay aligned with the original stops.
                # Geocoding the country alone pins an unknown town on the wrong city.
                entries.append(None)
                continue

            state = None if is_unknown(region) else region
            country = None if is_unknown(country) else country

            # Correct known renamed/relocated historical places before building the query
            resolved_city, resolved_country = resolve_historical_place(city, country)
            overridden = (resolved_city != city or resolved_country != country)
            # A historical override replaces the place's identity entirely, so a
            # region tied to the OLD name/country no longer applies
            if overridden:
                state = None
            city, country = resolved_city, resolved_country

            query = build_free_text_query(city, state, country)

            parsed_years = [y for y in (parse_year(y) for y in years) if y is not None]
            skip_pleiades = bool(parsed_years) and min(parsed_years) > PLEIADES_MAX_YEAR

            entries.append({
                "city": city, "state": state, "country": country, "query": query,
                "skip_pleiades": skip_pleiades,
            })

        return entries


    edu_p_results, edu_m_results, edu_mt_results, edu_g_results, edu_n_results, edu_w_results, edu_wp_results = [], [], [], [], [], [], []
    edu_agg_results = []

    print("Starting geocoding for Education locations...")
    for i, row in df.iterrows():
        raw_value = row.get("Education_Locations", None)
        entries = parse_education_locations(raw_value)

        if not entries:
            for lst in [edu_p_results, edu_m_results, edu_mt_results, edu_g_results, edu_n_results,
                        edu_w_results, edu_wp_results, edu_agg_results]:
                lst.append("Unknown")
            continue

        print(f"Row {sheet_row(i)}: processing {len(entries)} education location(s)")

        p_coords, m_coords, mt_coords, g_coords, n_coords, w_coords, wp_coords = [], [], [], [], [], [], []
        agg_coords = []
        for entry in entries:
            if entry is None:
                for bucket in (p_coords, m_coords, mt_coords, g_coords, n_coords, w_coords, wp_coords, agg_coords):
                    bucket.append("Unknown")
                continue
            res = geocode_place_multi(entry["query"], sheet_row(i), "Education_Locations",
                                       city=entry["city"], state=entry["state"], country=entry["country"],
                                       skip_pleiades=entry["skip_pleiades"])
            p_coords.append(res["pleiades"])
            m_coords.append(res["mapbox"])
            mt_coords.append(res["maptiler"])
            g_coords.append(res["geonames"])
            n_coords.append(res["nominatim"])
            w_coords.append(res["wikidata"])
            wp_coords.append(res["wikipedia"])

            agg_lat, agg_lon, _, _ = aggregate_coords(res)
            agg_lat, agg_lon, country_note = apply_country_check(agg_lat, agg_lon, entry["country"])
            if country_note:
                no_consensus.append((sheet_row(i), "Education_Locations", entry["query"], country_note))
                print(f"  {country_note}")
            if agg_lat == "Unknown" or agg_lon == "Unknown":
                agg_coords.append("Unknown")
            else:
                agg_coords.append(f"{agg_lat}, {agg_lon}")

        edu_p_results.append("; ".join(p_coords))
        edu_m_results.append("; ".join(m_coords))
        edu_mt_results.append("; ".join(mt_coords))
        edu_g_results.append("; ".join(g_coords))
        edu_n_results.append("; ".join(n_coords))
        edu_w_results.append("; ".join(w_coords))
        edu_wp_results.append("; ".join(wp_coords))
        edu_agg_results.append("; ".join(agg_coords))

    df["Education_Pleiades_Coords"] = edu_p_results
    df["Education_Mapbox_Coords"] = edu_m_results
    df["Education_MapTiler_Coords"] = edu_mt_results
    df["Education_GeoNames_Coords"] = edu_g_results
    df["Education_Nominatim_Coords"] = edu_n_results
    df["Education_Wikidata_Coords"] = edu_w_results
    df["Education_Wikipedia_Coords"] = edu_wp_results
    df["Education_Aggregate_Coordinates"] = edu_agg_results
    print("Finished geocoding Education locations.\n")

    # --- 5. Save result ----------------------------------------------------------
    df.to_csv(OUTPUT_CSV, index=False)
    print("Saved geocoded data to", OUTPUT_CSV)

    shutil.copy(OUTPUT_CSV, ARCHIVE_CSV)
    print("Also archived a copy to", ARCHIVE_CSV)

    # --- 6. Report failures, no-consensus cases, and disagreements ---------------
    if failed_lookups:
        print(f"\n{len(failed_lookups)} location(s) completely failed across ALL sources:")
        for row_num, field, query, reason in failed_lookups:
            print(f"  - Row {row_num} ({field}): '{query}'")
    else:
        print("\nAll locations yielded at least one valid geocode result.")

    if no_consensus:
        print(f"\n{len(no_consensus)} location(s) had NO CONSENSUS at all (no two sources agreed within "
              f"{CLUSTER_DISTANCE_KM}km) — aggregate left as \"Unknown\" rather than guessing. "
              f"These need manual review:")
        for row_num, field, query, note in no_consensus:
            print(f"  - Row {row_num} ({field}): '{query}' -> {note}")
    else:
        print("\nNo total-disagreement cases — every location had at least two sources agreeing with each other.")

    if disagreements:
        print(f"\n{len(disagreements)} location(s) had partial disagreement (a majority still won; worth a spot-check):")
        for row_num, field, query, note in disagreements:
            print(f"  - Row {row_num} ({field}): '{query}' -> {note}")
    else:
        print("\nNo partial disagreements detected between sources — all sources that returned a result agreed within "
              f"{CLUSTER_DISTANCE_KM}km of each other.")

    # --- 7. Close out the report file ---------------------------------------
    print(f"\nFull run log (everything above) saved to {REPORT_TXT}")
    sys.stdout = sys.__stdout__  # restore normal console output
    _report_file.close()

if __name__ == "__main__":
    _main()
