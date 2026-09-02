#!/usr/bin/env python3
"""sitemap-radar batch runner for GitHub Actions.
Fetches each competitor's sitemap, diffs vs. the snapshot committed last run,
writes updated snapshots + a markdown report per competitor.
"""
import gzip
import io
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

UA = "Mozilla/5.0 (compatible; OutSmartSitemapRadar/1.0; +https://out-smart.com)"
GROUP_DEPTH = 1
MAX_SITEMAPS = 50
MAX_URLS = 20000

COMPETITORS = [
    # PT
    {"name": "Infraspeak", "slug": "infraspeak-pt", "sitemap_url": "https://infraspeak.com/sitemap_pt_pt.xml"},
    {"name": "CentralGest", "slug": "centralgest", "sitemap_url": "https://www.centralgest.com/sitemap.xml"},
    {"name": "Synchroteam", "slug": "synchroteam", "sitemap_url": "https://www.synchroteam.com/synchroteam-com-sitemap.xml"},
    # NL
    {"name": "Bouwportaal", "slug": "bouwportaal", "sitemap_url": "https://bouwportaal.nl/sitemap.xml"},
    {"name": "FieldBuddy", "slug": "fieldbuddy", "sitemap_url": "https://fieldbuddy.com/sitemap_index.xml"},
    {"name": "Hero Software", "slug": "hero-software", "sitemap_url": "https://hero-software.nl/sitemaps-3-sitemap.xml"},
    {"name": "Plancraft", "slug": "plancraft", "sitemap_url": "https://plancraft.com/sitemap.xml"},
    {"name": "Robaws", "slug": "robaws", "sitemap_url": "https://robaws.com/sitemap.xml"},
    {"name": "Syntess", "slug": "syntess", "sitemap_url": "https://www.syntess.nl/sitemap_index.xml"},
    {"name": "Teamleader", "slug": "teamleader", "sitemap_url": "https://www.teamleader.eu/nl/sitemaps-1-sitemap.xml"},
    {"name": "vPlan", "slug": "vplan", "sitemap_url": "https://vplan.com/sitemap_index.xml"},
]


def local_tag(e):
    return e.tag.rsplit("}", 1)[-1].lower()


def fetch_bytes(url):
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=20) as resp:
        data = resp.read()
    if url.lower().endswith(".gz") or data[:2] == b"\x1f\x8b":
        data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
    return data


def parse_sitemap(xml_bytes):
    root = ElementTree.fromstring(xml_bytes)
    tag = local_tag(root)
    if tag == "sitemapindex":
        children = []
        for sm in root:
            if local_tag(sm) != "sitemap":
                continue
            for c in sm:
                if local_tag(c) == "loc" and c.text:
                    children.append(c.text.strip())
        return "index", children
    elif tag == "urlset":
        urls = []
        for u in root:
            if local_tag(u) != "url":
                continue
            loc = None
            lastmod = None
            for c in u:
                t = local_tag(c)
                if t == "loc" and c.text:
                    loc = c.text.strip()
                elif t == "lastmod" and c.text:
                    lastmod = c.text.strip()
            if loc:
                urls.append((loc, lastmod))
        return "urlset", urls
    raise ValueError(f"unrecognized root <{tag}>")


def collect(start_url):
    to_fetch = [start_url]
    seen = set()
    urls = {}
    while to_fetch and len(seen) < MAX_SITEMAPS:
        u = to_fetch.pop(0)
        if u in seen:
            continue
        seen.add(u)
        try:
            kind, entries = parse_sitemap(fetch_bytes(u))
        except Exception as e:
            print(f"WARN: failed {u}: {e}", file=sys.stderr)
            continue
        if kind == "index":
            to_fetch.extend(entries)
        else:
            for loc, lastmod in entries:
                if len(urls) >= MAX_URLS:
                    break
                urls[loc] = lastmod
        time.sleep(0.3)
    return urls


def directory_key(url, depth=GROUP_DEPTH):
    path = urlsplit(url).path
    segs = [s for s in path.split("/") if s]
    if not segs:
        return "(root)"
    return "/" + "/".join(segs[:depth]) + "/"


def process(competitor, snapshot_dir, report_dir):
    slug = competitor["slug"]
    snapshot_path = os.path.join(snapshot_dir, f"{slug}.json")
    report_path = os.path.join(report_dir, f"{slug}-latest.md")

    previous = None
    if os.path.exists(snapshot_path):
        with open(snapshot_path, "r", encoding="utf-8") as f:
            previous = json.load(f).get("urls", {})

    current = collect(competitor["sitemap_url"])

    lines = [f"# {competitor['name']} — sitemap diff", "",
             f"Bijgewerkt: {datetime.now(timezone.utc).isoformat()}", ""]

    if not current:
        lines.append("**FOUT**: geen URLs opgehaald — sitemap onbereikbaar of leeg deze run. "
                      "Vorige snapshot NIET overschreven.")
        os.makedirs(report_dir, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"{competitor['name']}: FOUT, geen URLs opgehaald", file=sys.stderr)
        return  # do not touch the snapshot file on failure

    os.makedirs(snapshot_dir, exist_ok=True)
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(
            {"source_url": competitor["sitemap_url"], "url_count": len(current), "urls": current},
            f, ensure_ascii=False, indent=2,
        )

    if previous is None:
        lines.append(f"**BASELINE** — {len(current)} URLs, nog geen vorige meting om mee te vergelijken.")
    else:
        prev_set, curr_set = set(previous), set(current)
        new_urls = sorted(curr_set - prev_set)
        removed_urls = sorted(prev_set - curr_set)
        shared = curr_set & prev_set
        lastmod_changed = sorted(u for u in shared if previous[u] != current[u])

        lines.append(
            f"NEW: {len(new_urls)}  |  REMOVED: {len(removed_urls)}  |  "
            f"LASTMOD_CHANGED: {len(lastmod_changed)} (zwak signaal)  |  "
            f"UNCHANGED: {len(shared) - len(lastmod_changed)}"
        )
        if shared and len(lastmod_changed) / len(shared) > 0.5:
            lines.append(
                "> Let op: vrijwel alle lastmod-datums zijn gewijzigd, ook al is er niets aan de "
                "URL's veranderd. Deze sitemap genereert `lastmod` waarschijnlijk dynamisch bij elke "
                "aanroep (huidige timestamp) i.p.v. echte content-wijzigingsdatums — LASTMOD_CHANGED "
                "hierboven is voor deze concurrent dus betekenisloos, negeer het volledig."
            )
        lines.append("")

        by_dir = defaultdict(lambda: {"NEW": [], "REMOVED": []})
        for u in new_urls:
            by_dir[directory_key(u)]["NEW"].append(u)
        for u in removed_urls:
            by_dir[directory_key(u)]["REMOVED"].append(u)

        for d, s in sorted(by_dir.items(), key=lambda kv: -(len(kv[1]["NEW"]) + len(kv[1]["REMOVED"]))):
            if not s["NEW"] and not s["REMOVED"]:
                continue
            lines.append(f"## {d}  (+{len(s['NEW'])} / -{len(s['REMOVED'])})")
            for u in s["NEW"]:
                lines.append(f"- NEW: {u}")
            for u in s["REMOVED"]:
                lines.append(f"- REMOVED: {u}")
            lines.append("")

    os.makedirs(report_dir, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"{competitor['name']}: {len(current)} URLs, report -> {report_path}")


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    snapshot_dir = os.path.join(root, "snapshots")
    report_dir = os.path.join(root, "reports")
    for competitor in COMPETITORS:
        process(competitor, snapshot_dir, report_dir)


if __name__ == "__main__":
    main()
