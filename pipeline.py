#!/usr/bin/env python3
"""
Nightly pipeline: build data.json of streaming titles sorted by IMDb rating.

Data sources:
  1. IMDb non-commercial dataset (title.ratings.tsv.gz) - ratings + vote counts
  2. TMDB API - catalogs per streaming service, metadata, posters, IMDb ID mapping

Usage:
  export TMDB_API_KEY=your_key_here     (get one free at themoviedb.org/settings/api)
  pip install requests
  python pipeline.py

Output:
  data.json  - all titles with rating >= MIN_RATING and votes >= MIN_VOTES,
               sorted by IMDb rating descending. Serve next to index.html.
  cache.db   - SQLite cache of TMDB->IMDb ID mappings (makes reruns much faster)
"""

import csv
import gzip
import io
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ----------------------------- Configuration -----------------------------

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "")   # optional: adds Rotten Tomatoes scores
WATCH_REGION = "US"
MIN_VOTES = 1000        # skip titles with fewer IMDb votes
MIN_RATING = 0.0        # keep everything; frontend filters at 6.0 by default
MAX_PAGES = 500         # TMDB hard limit per discover query

# TMDB watch-provider IDs. Full list: GET /watch/providers/movie?watch_region=US
PROVIDERS = {
    "Netflix": 8,
    "Prime Video": 9,
    "Disney+": 337,
    "Hulu": 15,
    "Max": 1899,
    "Apple TV+": 350,
    "Paramount+": 531,
    "Peacock": 386,
}

IMDB_RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
OMDB_URL = "https://www.omdbapi.com/"
TMDB_BASE = "https://api.themoviedb.org/3"
POSTER_BASE = "https://image.tmdb.org/t/p/w342"

OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
CACHE_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.db")

session = requests.Session()


# ----------------------------- Helpers -----------------------------

def tmdb_get(path, **params):
    """GET a TMDB endpoint with retry on rate limit."""
    params["api_key"] = TMDB_API_KEY
    for attempt in range(5):
        r = session.get(f"{TMDB_BASE}{path}", params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 2)))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"TMDB rate limit persisted for {path}")


def load_imdb_ratings():
    """Download IMDb ratings dump -> {imdb_id: (rating, votes)} for titles >= MIN_VOTES."""
    print("Downloading IMDb ratings dataset...")
    r = session.get(IMDB_RATINGS_URL, timeout=120)
    r.raise_for_status()
    ratings = {}
    with gzip.open(io.BytesIO(r.content), "rt", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            votes = int(row["numVotes"])
            if votes >= MIN_VOTES:
                ratings[row["tconst"]] = (float(row["averageRating"]), votes)
    print(f"  {len(ratings):,} titles with >= {MIN_VOTES} votes")
    return ratings


def load_genre_map():
    """TMDB genre id -> name, for both movies and TV."""
    genres = {}
    for kind in ("movie", "tv"):
        for g in tmdb_get(f"/genre/{kind}/list")["genres"]:
            genres[g["id"]] = g["name"]
    return genres


def load_provider_ids():
    """Map each service to ALL matching TMDB provider ids, including channel
    variants like 'Paramount+ Amazon Channel' (which have separate ids)."""
    def norm(name):
        return name.lower().replace(" plus", "+")
    matched = {service: {pid} for service, pid in PROVIDERS.items()}
    for kind in ("movie", "tv"):
        for p in tmdb_get(f"/watch/providers/{kind}", watch_region=WATCH_REGION)["results"]:
            pname = norm(p["provider_name"])
            for service in PROVIDERS:
                if pname.startswith(norm(service)):
                    matched[service].add(p["provider_id"])
    for service, ids in matched.items():
        if len(ids) > 1:
            print(f"  {service}: including {len(ids)} provider variants {sorted(ids)}")
    return matched


def discover_catalog(kind, provider_ids):
    """All titles on one service via TMDB Discover. kind: 'movie' or 'tv'."""
    results, page, total = {}, 1, 1
    while page <= total and page <= MAX_PAGES:
        data = tmdb_get(
            f"/discover/{kind}",
            with_watch_providers="|".join(str(i) for i in provider_ids),
            watch_region=WATCH_REGION,
            with_watch_monetization_types="flatrate|free|ads",
            sort_by="popularity.desc",
            page=page,
        )
        total = data.get("total_pages", 1)
        for item in data.get("results", []):
            results[item["id"]] = item
        page += 1
    return results


# ----------------------------- IMDb ID cache -----------------------------

def cache_init():
    db = sqlite3.connect(CACHE_DB)
    db.execute(
        "CREATE TABLE IF NOT EXISTS imdb_map "
        "(kind TEXT, tmdb_id INTEGER, imdb_id TEXT, PRIMARY KEY (kind, tmdb_id))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS omdb_info "
        "(imdb_id TEXT PRIMARY KEY, rt INTEGER, rated TEXT, runtime INTEGER, mc INTEGER, "
        "actors TEXT, director TEXT, awards TEXT, box_office TEXT)"
    )
    # migrate older cache.db files that lack newer columns
    for col in ("mc INTEGER", "actors TEXT", "director TEXT", "awards TEXT", "box_office TEXT"):
        try:
            db.execute(f"ALTER TABLE omdb_info ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    db.execute(
        "CREATE TABLE IF NOT EXISTS tmdb_extra "
        "(kind TEXT, tmdb_id INTEGER, episodes INTEGER, trailer TEXT, "
        "PRIMARY KEY (kind, tmdb_id))"
    )
    return db


def fetch_imdb_id(kind, tmdb_id):
    data = tmdb_get(f"/{kind}/{tmdb_id}/external_ids")
    return kind, tmdb_id, data.get("imdb_id") or ""


def resolve_imdb_ids(db, kind, tmdb_ids):
    """Return {tmdb_id: imdb_id}, using cache and fetching only what's missing."""
    cached = dict(
        db.execute(
            f"SELECT tmdb_id, imdb_id FROM imdb_map WHERE kind=? "
            f"AND tmdb_id IN ({','.join('?' * len(tmdb_ids))})",
            [kind, *tmdb_ids],
        ).fetchall()
    ) if tmdb_ids else {}
    missing = [t for t in tmdb_ids if t not in cached]
    print(f"  {kind}: {len(cached):,} cached, {len(missing):,} to fetch")
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(fetch_imdb_id, kind, t) for t in missing]
        for i, fut in enumerate(as_completed(futures), 1):
            k, tid, iid = fut.result()
            cached[tid] = iid
            db.execute("INSERT OR REPLACE INTO imdb_map VALUES (?,?,?)", (k, tid, iid))
            if i % 500 == 0:
                db.commit()
                print(f"    fetched {i:,}/{len(missing):,}")
    db.commit()
    return cached


# ----------------------------- Rotten Tomatoes (via OMDb) -----------------------------

class OmdbError(Exception):
    """OMDb rejected the request (invalid/unactivated key, or daily limit)."""


# column order in the omdb_info table (after imdb_id)
OMDB_FIELDS = ("rt", "rated", "runtime", "mc", "actors", "director", "awards", "box_office")

# not-yet-fetched rows have actors=NULL; fetched-but-absent values are stored as ""
_OMDB_EMPTY = {"rt": -1, "rated": "", "runtime": None, "mc": -1,
               "actors": "", "director": "", "awards": "", "box_office": ""}


def fetch_omdb_info(imdb_id):
    """(imdb_id, info_dict) via OMDb. rt/mc use -1 and text fields use '' for 'none'."""
    for attempt in range(3):
        try:
            r = session.get(OMDB_URL, params={"i": imdb_id, "apikey": OMDB_API_KEY}, timeout=30)
            break
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2)   # transient network hiccup - retry
    if r.status_code == 401:
        try:
            msg = r.json().get("Error", "")
        except ValueError:
            msg = ""
        raise OmdbError(msg or "401 Unauthorized")
    info = dict(_OMDB_EMPTY)
    try:
        data = r.json()
    except ValueError:
        return imdb_id, info   # OMDb occasionally returns broken JSON - skip it

    def clean(field):
        v = data.get(field) or ""
        return "" if v == "N/A" else v

    for entry in data.get("Ratings", []):
        if entry.get("Source") == "Rotten Tomatoes":
            try:
                info["rt"] = int(entry["Value"].rstrip("%"))
            except ValueError:
                pass
    info["rated"] = clean("Rated")
    r_str = data.get("Runtime") or ""
    if r_str.endswith(" min"):
        try:
            info["runtime"] = int(r_str[:-4].replace(",", ""))
        except ValueError:
            pass
    ms = data.get("Metascore") or ""
    info["mc"] = int(ms) if ms.isdigit() else -1
    info["actors"] = clean("Actors")
    info["director"] = clean("Director")
    info["awards"] = clean("Awards")
    info["box_office"] = clean("BoxOffice")
    return imdb_id, info


def resolve_omdb_info(db, imdb_ids):
    """{imdb_id: (rt, rated, runtime)}. Cached in SQLite; rt=-1 means 'no RT score exists'.
    Stops gracefully when OMDb's daily limit is hit and resumes on the next run."""
    if not OMDB_API_KEY:
        print("OMDB_API_KEY not set - skipping Rotten Tomatoes / rated / runtime")
        return {}

    # Sanity-check the key with one known title before doing anything else
    try:
        _, test = fetch_omdb_info("tt0111161")  # Shawshank
        print(f"  OMDb key OK (test: RT={test['rt']}%, MC={test['mc']}, "
              f"rated {test['rated']}, dir. {test['director']})")
    except OmdbError as e:
        msg = str(e)
        if "invalid" in msg.lower():
            print(f"  OMDb says: {msg}")
            print("  -> Your key is invalid or not yet activated. Check the email from")
            print("     OMDb and click its activation link, and confirm OMDB_API_KEY is")
            print("     set to the key exactly (no quotes/spaces). Skipping this run.")
        else:
            print(f"  OMDb says: {msg} - daily limit used up, will resume next run.")
        return {}

    cols = ", ".join(OMDB_FIELDS)
    cached = {row[0]: dict(zip(OMDB_FIELDS, row[1:])) for row in
              db.execute(f"SELECT imdb_id, {cols} FROM omdb_info")}
    # rows with actors=NULL were cached before the newer columns existed - refetch those
    missing = [i for i in imdb_ids if i not in cached or cached[i]["actors"] is None]
    if any(i in cached for i in missing):
        print("  Note: refetching previously cached titles once to pick up cast/director/awards")
    print(f"OMDb info: {len(cached):,} cached, {len(missing):,} to fetch")
    done = 0
    try:
        for start in range(0, len(missing), 200):
            chunk = missing[start:start + 200]
            with ThreadPoolExecutor(max_workers=5) as pool:
                for imdb_id, info in pool.map(fetch_omdb_info, chunk):
                    cached[imdb_id] = info
                    db.execute(
                        "INSERT OR REPLACE INTO omdb_info VALUES (?,?,?,?,?,?,?,?,?)",
                        (imdb_id, *(info[f] for f in OMDB_FIELDS)))
            db.commit()
            done += len(chunk)
            if done % 1000 == 0:
                print(f"  fetched {done:,}/{len(missing):,}")
    except OmdbError as e:
        print(f"  OMDb stopped responding after {done:,} ({e}) - partial data saved, "
              "the rest will fill in on future runs")
    db.commit()
    return cached


def fetch_tmdb_extra(kind, tmdb_id):
    """(kind, tmdb_id, episodes, trailer_youtube_key) via TMDB details+videos."""
    try:
        data = tmdb_get(f"/{kind}/{tmdb_id}", append_to_response="videos")
    except requests.HTTPError:
        return kind, tmdb_id, 0, ""
    episodes = data.get("number_of_episodes") or 0
    trailer = ""
    videos = [v for v in data.get("videos", {}).get("results", [])
              if v.get("site") == "YouTube" and v.get("type") == "Trailer"]
    if videos:
        # prefer official trailers, then most recent
        videos.sort(key=lambda v: (not v.get("official"), v.get("published_at") or ""))
        trailer = videos[0].get("key") or ""
    return kind, tmdb_id, episodes, trailer


def resolve_tmdb_extra(db, pairs):
    """{(kind, tmdb_id): (episodes, trailer_key)} via TMDB details. Cached."""
    cached = {(k, t): (e, tr) for k, t, e, tr in
              db.execute("SELECT kind, tmdb_id, episodes, trailer FROM tmdb_extra")}
    missing = [p for p in pairs if p not in cached]
    print(f"TMDB details (episodes/trailers): {len(cached):,} cached, {len(missing):,} to fetch")
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(fetch_tmdb_extra, k, t) for k, t in missing]
        for i, fut in enumerate(as_completed(futures), 1):
            k, t, eps, trailer = fut.result()
            cached[(k, t)] = (eps, trailer)
            db.execute("INSERT OR REPLACE INTO tmdb_extra VALUES (?,?,?,?)", (k, t, eps, trailer))
            if i % 500 == 0:
                db.commit()
                print(f"  fetched {i:,}/{len(missing):,}")
    db.commit()
    return cached


# ----------------------------- Main -----------------------------

def main():
    if not TMDB_API_KEY:
        sys.exit("Set TMDB_API_KEY environment variable first. "
                 "Get a free key at https://www.themoviedb.org/settings/api")

    ratings = load_imdb_ratings()
    genre_map = load_genre_map()
    print("Resolving provider variants...")
    provider_map = load_provider_ids()
    db = cache_init()

    # titles[(kind, tmdb_id)] = {"item": tmdb_result, "services": set()}
    titles = {}
    for service, pids in provider_map.items():
        for kind in ("movie", "tv"):
            print(f"Discovering {kind}s on {service}...")
            catalog = discover_catalog(kind, pids)
            print(f"  {len(catalog):,} titles")
            for tmdb_id, item in catalog.items():
                key = (kind, tmdb_id)
                titles.setdefault(key, {"item": item, "services": set()})
                titles[key]["services"].add(service)

    # Resolve IMDb IDs per kind
    imdb_ids = {}
    for kind in ("movie", "tv"):
        ids = [tid for (k, tid) in titles if k == kind]
        imdb_ids[kind] = resolve_imdb_ids(db, kind, ids)

    # Join and filter
    out = []
    for (kind, tmdb_id), entry in titles.items():
        imdb_id = imdb_ids[kind].get(tmdb_id)
        if not imdb_id or imdb_id not in ratings:
            continue
        rating, votes = ratings[imdb_id]
        if rating < MIN_RATING:
            continue
        item = entry["item"]
        date = item.get("release_date") or item.get("first_air_date") or ""
        out.append({
            "imdb_id": imdb_id,
            "tmdb_id": tmdb_id,
            "title": item.get("title") or item.get("name") or "",
            "type": "movie" if kind == "movie" else "tv",
            "year": int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None,
            "imdb_rating": rating,
            "imdb_votes": votes,
            "genres": [genre_map.get(g, "") for g in item.get("genre_ids", []) if g in genre_map],
            "language": item.get("original_language") or "en",
            "services": sorted(entry["services"]),
            "overview": item.get("overview") or "",
            "poster": POSTER_BASE + item["poster_path"] if item.get("poster_path") else None,
        })

    # Attach OMDb data: RT score, content rating, movie runtime (needs OMDB_API_KEY)
    omdb = resolve_omdb_info(db, [t["imdb_id"] for t in out])
    for t in out:
        info = omdb.get(t["imdb_id"]) or _OMDB_EMPTY
        t["rt"] = info["rt"] if info["rt"] is not None and info["rt"] >= 0 else None
        t["rated"] = info["rated"] or None
        t["runtime"] = info["runtime"] if t["type"] == "movie" else None
        t["mc"] = info["mc"] if info["mc"] is not None and info["mc"] >= 0 else None
        t["actors"] = info["actors"] or None
        t["director"] = info["director"] or None
        t["awards"] = info["awards"] or None
        t["box_office"] = (info["box_office"] or None) if t["type"] == "movie" else None

    # Attach episode counts + trailer links (TMDB details, cached)
    extra = resolve_tmdb_extra(db, [(t["type"], t["tmdb_id"]) for t in out])
    for t in out:
        eps, trailer = extra.get((t["type"], t["tmdb_id"]), (None, None))
        t["episodes"] = (eps or None) if t["type"] == "tv" else None
        t["trailer"] = trailer or None

    out.sort(key=lambda t: (-t["imdb_rating"], -t["imdb_votes"]))
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "titles": out}, f)
    print(f"\nWrote {len(out):,} titles to {OUT_FILE}")
    db.close()

    # Regenerate static SEO landing pages from the fresh data
    try:
        import generate_pages
        generate_pages.build(OUT_FILE)
    except ImportError:
        print("generate_pages.py not found - skipping landing pages")


if __name__ == "__main__":
    main()
