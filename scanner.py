#!/usr/bin/env python3
from __future__ import annotations
import asyncio, json, re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

ROOT = Path(__file__).resolve().parent
SOURCES = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
OUTPUT = ROOT / "docs" / "data.json"
HOURS = 24

PATTERNS = [
    r"\biran(?:ian|ians)?\b", r"\btehran\b", r"\bislamic republic\b",
    r"\birgc\b", r"\brevolutionary guards?\b", r"\bkhamenei\b",
    r"\bpezeshkian\b", r"\bnatanz\b", r"\bfordow\b",
    r"\bstr(?:ait)?\.?\s+of\s+hormuz\b", r"\bquds force\b",
    r"\bbasij\b", r"\biran nuclear\b"
]
IRAN_RE = re.compile("|".join(PATTERNS), re.I)
FEED_PATHS = ["/feed/", "/feed", "/rss", "/rss.xml", "/feed.xml",
              "/atom.xml", "/index.xml", "/feeds/all"]

def now():
    return datetime.now(timezone.utc)

def clean(value):
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)

def parse_date(value):
    if not value:
        return None
    try:
        dt = dateparser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

async def fetch(session, url):
    try:
        async with session.get(url, timeout=25, allow_redirects=True) as r:
            if r.status >= 400:
                return "", ""
            return await r.text(errors="ignore"), r.headers.get("content-type", "")
    except Exception:
        return "", ""

async def discover_feeds(session, homepage):
    html, _ = await fetch(session, homepage)
    found = set()
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("link", href=True):
            rel = " ".join(tag.get("rel", [])).lower()
            typ = (tag.get("type") or "").lower()
            if "alternate" in rel and ("rss" in typ or "atom" in typ or "xml" in typ):
                found.add(urljoin(homepage, tag["href"]))
    found.update(urljoin(homepage, p) for p in FEED_PATHS)
    return list(found)

async def scan_feed(session, source, feed_url, cutoff):
    raw, ctype = await fetch(session, feed_url)
    head = raw[:700].lower()
    if not raw or ("<rss" not in head and "<feed" not in head and "xml" not in ctype.lower()):
        return []
    parsed = feedparser.parse(raw)
    results = []
    for entry in parsed.entries:
        title = clean(entry.get("title"))
        summary = clean(entry.get("summary") or entry.get("description"))
        published = parse_date(entry.get("published") or entry.get("updated") or entry.get("created"))
        url = entry.get("link", "")
        if not published or published < cutoff or published > now() + timedelta(hours=2):
            continue
        text = title + "\n" + summary
        matches = sorted({m.group(0) for m in IRAN_RE.finditer(text)}, key=str.lower)
        if not matches:
            continue
        results.append({
            "source": source["name"],
            "title": title,
            "url": url,
            "published_utc": published.isoformat(),
            "summary": summary[:900],
            "matched_terms": matches,
        })
    return results

async def scan_source(session, source, cutoff):
    feeds = await discover_feeds(session, source["url"])
    groups = await asyncio.gather(
        *(scan_feed(session, source, u, cutoff) for u in feeds),
        return_exceptions=True
    )
    items = []
    for group in groups:
        if isinstance(group, list):
            items.extend(group)
    return items

async def main():
    cutoff = now() - timedelta(hours=HOURS)
    headers = {"User-Agent": "IranThinkTankMonitor/1.0 (+public-interest research monitor)"}
    connector = aiohttp.TCPConnector(limit=15)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        groups = await asyncio.gather(
            *(scan_source(session, s, cutoff) for s in SOURCES),
            return_exceptions=True
        )

    unique = {}
    checked = []
    for source, group in zip(SOURCES, groups):
        checked.append(source["name"])
        if not isinstance(group, list):
            continue
        for item in group:
            key = item["url"].rstrip("/") or item["source"] + "|" + item["title"].lower()
            unique[key] = item

    items = sorted(unique.values(), key=lambda x: x["published_utc"], reverse=True)
    payload = {
        "generated_utc": now().isoformat(),
        "window_hours": HOURS,
        "source_count": len(SOURCES),
        "item_count": len(items),
        "items": items,
    }
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(items)} items to {OUTPUT}")

if __name__ == "__main__":
    asyncio.run(main())
