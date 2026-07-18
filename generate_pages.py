#!/usr/bin/env python3
"""
Static SEO landing page generator for FlopFilter.

Reads data.json and writes:
  /<service>/index.html                 e.g. /netflix/
  /<service>/<genre>/index.html         e.g. /netflix/comedy/  (only if enough titles)
  /sitemap.xml
  /robots.txt

Called automatically at the end of pipeline.py, or run standalone:
  python generate_pages.py
"""

import html
import json
import os
import re
import shutil

BASE_URL = "https://flopfilter.com"
MIN_RATING = 6.0          # pages only show titles meeting the site's bar
MIN_TITLES_FOR_GENRE = 12 # skip service+genre combos thinner than this
TOP_N = 50                # titles per page

PLAUSIBLE = '<script defer data-domain="flopfilter.com" src="https://plausible.io/js/script.js"></script>'

CSS = """
:root{--bg:#0e1117;--panel:#161b26;--panel2:#1d2433;--border:#2a3347;--text:#e8ecf4;
--muted:#8b95a8;--accent:#f5c518;--accent2:#4f8cff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:20px 28px}header a{text-decoration:none}
header h1{font-size:22px;color:var(--text);display:inline}header h1 span{color:var(--accent)}
.wrap{max-width:860px;margin:0 auto;padding:0 20px 60px}
h2{font-size:24px;margin:18px 0 10px}
.intro{color:var(--muted);margin-bottom:8px}
.updated{color:var(--muted);font-size:12px;margin-bottom:24px}
.cta{display:inline-block;margin:6px 0 26px;color:var(--accent2);text-decoration:none;
border:1px solid var(--border);border-radius:8px;padding:8px 16px}
.cta:hover{border-color:var(--accent2)}
.item{display:flex;gap:16px;background:var(--panel);border:1px solid var(--border);
border-radius:12px;padding:14px;margin-bottom:10px}
.rank{color:var(--muted);font-size:18px;font-weight:700;min-width:34px;text-align:right}
.item img{width:64px;height:96px;object-fit:cover;border-radius:8px;background:var(--panel2)}
.t{font-weight:600;font-size:16px}.t .yr{color:var(--muted);font-weight:400}
.scores{font-size:13px;margin:2px 0}.scores .imdb{color:var(--accent);font-weight:700}
.scores .rt{color:#fa5342;font-weight:600}.scores .mcv{color:#66cc33;font-weight:600}
.scores .meta{color:var(--muted)}
.ov{font-size:13px;color:#b9c2d4;margin-top:4px}
.also{margin:30px 0 0}.also h3{font-size:15px;margin-bottom:8px}
.also a{display:inline-block;color:var(--accent2);text-decoration:none;font-size:13px;
border:1px solid var(--border);border-radius:999px;padding:4px 12px;margin:0 6px 8px 0}
.also a:hover{border-color:var(--accent2)}
footer{max-width:860px;margin:0 auto;padding:24px 20px 40px;border-top:1px solid var(--border);
color:var(--muted);font-size:12px;line-height:1.8;text-align:center}footer a{color:var(--muted)}
"""

FOOTER = """<footer>
&copy; 2026 FlopFilter. All rights reserved.<br>
This product uses the TMDB API but is not endorsed or certified by
<a href="https://www.themoviedb.org" target="_blank" rel="noopener">TMDB</a>.
Streaming availability data provided by
<a href="https://www.justwatch.com" target="_blank" rel="noopener">JustWatch</a>.<br>
Ratings information courtesy of
<a href="https://www.imdb.com" target="_blank" rel="noopener">IMDb</a>. Used with permission.
Rotten Tomatoes&reg; scores via the
<a href="https://www.omdbapi.com" target="_blank" rel="noopener">OMDb API</a>.
</footer>"""


def slugify(name):
    s = name.lower().replace("+", " plus").replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def esc(s):
    return html.escape(str(s), quote=True)


def fmt_runtime(m):
    if m >= 60:
        return f"{m // 60}h {m % 60}m" if m % 60 else f"{m // 60}h"
    return f"{m}m"


def item_html(rank, t):
    yr = f' <span class="yr">({t["year"]})</span>' if t.get("year") else ""
    scores = [f'<span class="imdb">&#9733; {t["imdb_rating"]:.1f} IMDb</span>']
    if t.get("rt") is not None:
        scores.append(f'<span class="rt">&#127813; {t["rt"]}%</span>')
    if t.get("mc") is not None:
        scores.append(f'<span class="mcv">MC {t["mc"]}</span>')
    meta = []
    if t.get("rated"):
        meta.append(esc(t["rated"]))
    if t["type"] == "movie" and t.get("runtime"):
        meta.append(fmt_runtime(t["runtime"]))
    if t["type"] == "tv" and t.get("episodes"):
        meta.append(f'{t["episodes"]} episodes')
    meta.extend(t.get("genres", []))
    if meta:
        scores.append(f'<span class="meta">{esc(" · ".join(meta))}</span>')
    poster = (f'<img src="{esc(t["poster"])}" alt="{esc(t["title"])} poster" '
              f'loading="lazy" width="64" height="96">') if t.get("poster") else ""
    ov = esc((t.get("overview") or "")[:220])
    return (f'<div class="item"><div class="rank">{rank}</div>{poster}<div>'
            f'<div class="t">{esc(t["title"])}{yr}</div>'
            f'<div class="scores">{" &nbsp; ".join(scores)}</div>'
            f'<div class="ov">{ov}</div></div></div>')


def render_page(*, canonical, title_tag, meta_desc, h1, intro, titles, also_links, date):
    items = "\n".join(item_html(i + 1, t) for i, t in enumerate(titles[:TOP_N]))
    also = ""
    if also_links:
        links = "\n".join(f'<a href="{esc(u)}">{esc(label)}</a>' for label, u in also_links)
        also = f'<div class="also"><h3>More on FlopFilter</h3>{links}</div>'
    ld = json.dumps({
        "@context": "https://schema.org", "@type": "ItemList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": t["title"]}
            for i, t in enumerate(titles[:TOP_N])
        ]}).replace("<", "\\u003c")   # keep raw '<' out of the <script> block
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title_tag)}</title>
<meta name="description" content="{esc(meta_desc)}">
<link rel="canonical" href="{esc(canonical)}">
{PLAUSIBLE}
<script type="application/ld+json">{ld}</script>
<style>{CSS}</style>
</head>
<body>
<header><a href="/"><h1>Flop<span>Filter</span></h1></a></header>
<div class="wrap">
<h2>{esc(h1)}</h2>
<p class="intro">{intro}</p>
<p class="updated">Updated nightly &middot; last refresh {esc(date)} &middot; minimum 1,000 IMDb votes &middot; nothing under {MIN_RATING}</p>
<a class="cta" href="/">Filter &amp; sort everything yourself on FlopFilter &rarr;</a>
{items}
{also}
</div>
{FOOTER}
</body>
</html>"""


def build(data_path):
    root = os.path.dirname(os.path.abspath(data_path))
    with open(data_path, encoding="utf-8") as f:
        data = json.load(f)
    date = (data.get("generated") or "")[:10]
    titles = [t for t in data["titles"] if t["imdb_rating"] >= MIN_RATING]

    services = sorted({s for t in titles for s in t["services"]})
    pages = []  # (url_path,) for sitemap

    for service in services:
        s_slug = slugify(service)
        s_dir = os.path.join(root, s_slug)
        shutil.rmtree(s_dir, ignore_errors=True)
        os.makedirs(s_dir, exist_ok=True)

        s_titles = [t for t in titles if service in t["services"]]
        s_titles.sort(key=lambda t: (-t["imdb_rating"], -t["imdb_votes"]))

        genres = {}
        for t in s_titles:
            for g in t.get("genres", []):
                genres.setdefault(g, []).append(t)
        genre_pages = sorted(g for g, ts in genres.items() if len(ts) >= MIN_TITLES_FOR_GENRE)

        also = [(f"Best {g} on {service}", f"/{s_slug}/{slugify(g)}/") for g in genre_pages]
        also += [(f"Best on {s2}", f"/{slugify(s2)}/") for s2 in services if s2 != service]

        top3 = ", ".join(t["title"] for t in s_titles[:3])
        with open(os.path.join(s_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(render_page(
                canonical=f"{BASE_URL}/{s_slug}/",
                title_tag=f"Best Movies & TV Shows on {service}, Ranked by IMDb Rating | FlopFilter",
                meta_desc=(f"The {min(len(s_titles), TOP_N)} highest-rated movies and TV shows "
                           f"streaming on {service} right now, ranked by IMDb rating and updated "
                           f"nightly. Topping the list: {top3}."),
                h1=f"Best of {service}, ranked by IMDb rating",
                intro=(f"All {len(s_titles):,} titles streaming on {service} with an IMDb rating "
                       f"of {MIN_RATING}+ — the top {min(len(s_titles), TOP_N)} are ranked below."),
                titles=s_titles, also_links=also, date=date))
        pages.append(f"/{s_slug}/")

        for g in genre_pages:
            g_slug = slugify(g)
            g_dir = os.path.join(s_dir, g_slug)
            os.makedirs(g_dir, exist_ok=True)
            g_titles = genres[g]
            g_titles.sort(key=lambda t: (-t["imdb_rating"], -t["imdb_votes"]))
            g_top3 = ", ".join(t["title"] for t in g_titles[:3])
            g_also = ([(f"All of {service}", f"/{s_slug}/")] +
                      [(f"Best {g2} on {service}", f"/{s_slug}/{slugify(g2)}/")
                       for g2 in genre_pages if g2 != g])
            with open(os.path.join(g_dir, "index.html"), "w", encoding="utf-8") as f:
                f.write(render_page(
                    canonical=f"{BASE_URL}/{s_slug}/{g_slug}/",
                    title_tag=f"Best {g} on {service}, Ranked by IMDb Rating | FlopFilter",
                    meta_desc=(f"The highest-rated {g.lower()} titles streaming on {service}, "
                               f"ranked by IMDb rating and updated nightly. "
                               f"Leading the pack: {g_top3}."),
                    h1=f"Best {g} on {service}, ranked by IMDb rating",
                    intro=(f"{len(g_titles):,} {g.lower()} titles on {service} rate "
                           f"{MIN_RATING}+ on IMDb — the top {min(len(g_titles), TOP_N)} "
                           f"are ranked below."),
                    titles=g_titles, also_links=g_also, date=date))
            pages.append(f"/{s_slug}/{g_slug}/")

    # sitemap.xml + robots.txt
    urls = [f"{BASE_URL}/"] + [f"{BASE_URL}{p}" for p in pages]
    sitemap = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        sitemap.append(f"  <url><loc>{esc(u)}</loc><lastmod>{esc(date)}</lastmod></url>")
    sitemap.append("</urlset>")
    with open(os.path.join(root, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(sitemap))
    with open(os.path.join(root, "robots.txt"), "w", encoding="utf-8") as f:
        f.write(f"User-agent: *\nAllow: /\n\nSitemap: {BASE_URL}/sitemap.xml\n")

    print(f"Generated {len(pages)} landing pages + sitemap.xml + robots.txt")
    return pages


if __name__ == "__main__":
    build(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json"))
