# FlopFilter — starter kit

Find what's actually worth watching: every title on your streaming services, sorted by IMDb or Rotten Tomatoes rating.

## Files

- `pipeline.py` — nightly data pipeline (IMDb ratings dump + TMDB catalogs → `data.json`)
- `index.html` — the website (single file, no build step). Works standalone with sample data until you generate `data.json`.

## Setup (one time)

1. Get a free TMDB API key: https://www.themoviedb.org/settings/api
2. Optional (Rotten Tomatoes scores): get an OMDb key at https://www.omdbapi.com/apikey.aspx —
   free tier is 1,000 requests/day (initial backfill takes weeks); their Patreon tier (a few $/mo)
   raises the cap so the backfill finishes in one run
3. `pip install requests`

## Run the pipeline (PowerShell)

```powershell
$env:TMDB_API_KEY = "your_tmdb_key"
$env:OMDB_API_KEY = "your_omdb_key"    # optional; skip to run without RT scores
python pipeline.py
```

(Mac/Linux: `export TMDB_API_KEY=...` instead. Use `setx` on Windows to persist the keys.)

First run takes a while (it fetches the IMDb→TMDB ID mapping for every title, ~10–20k API calls). Mappings are cached in `cache.db`, so later runs only fetch new titles — a nightly run finishes in a few minutes.

## View the site

`data.json` must be served over HTTP (browsers block `fetch` from `file://`):

```bash
python -m http.server 8000
# open http://localhost:8000
```

## Nightly automation

- Linux/Mac: cron — `0 3 * * * cd /path/to/project && TMDB_API_KEY=xxx python pipeline.py`
- Windows: Task Scheduler
- Or a GitHub Action on a schedule that commits `data.json` and deploys to GitHub Pages / Netlify — free hosting, no server.

## Configuration (top of pipeline.py)

- `PROVIDERS` — which services to include (TMDB provider IDs)
- `MIN_VOTES` — currently 1,000, per your spec
- `WATCH_REGION` — currently US

## Licensing notes

- IMDb datasets: personal/non-commercial use only
- TMDB API + watch provider data: free non-commercial; provider data is sourced from JustWatch and requires attribution if you publish
- If you ever monetize, look at Watchmode (api.watchmode.com) for licensed availability data
