#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

ROOT = Path(__file__).resolve().parent
SOURCES = [s for s in json.loads((ROOT / "sources.json").read_text(encoding="utf-8")) if s.get("enabled", True)]
OUTPUT = ROOT / "docs" / "data.json"
SCAN_HOURS = 72
ARCHIVE_DAYS = 30
MAX_SITEMAP_URLS = 35
MAX_ARTICLE_FETCHES_PER_SOURCE = 12

PATTERNS = [
    r"\biran(?:ian|ians)?\b", r"\btehran\b", r"\bislamic republic\b",
    r"\birgc\b", r"\brevolutionary guards?\b", r"\bkhamenei\b",
    r"\bpezeshkian\b", r"\bnatanz\b", r"\bfordow\b", r"\barak\b",
    r"\bstr(?:ait)?\.?\s+of\s+hormuz\b", r"\bquds force\b",
    r"\bbasij\b", r"\biran nuclear\b", r"\bpersian gulf\b"
]
IRAN_RE = re.compile("|".join(PATTERNS), re.I)
FEED_PATHS = [
    "/feed/", "/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml",
    "/index.xml", "/feeds/all", "/news/rss", "/en/rss.xml"
]

TOPICS = {
    "Nuclear": ["nuclear", "uranium", "enrichment", "iaea", "fordow", "natanz", "centrifuge", "safeguards"],
    "US–Iran": ["united states", "u.s.", "washington", "trump", "american", "us-iran", "us–iran"],
    "Israel": ["israel", "israeli", "idf", "netanyahu", "tel aviv"],
    "Sanctions": ["sanction", "embargo", "asset freeze", "maximum pressure", "export control"],
    "IRGC": ["irgc", "revolutionary guard", "quds force", "basij"],
    "Missiles & Drones": ["missile", "ballistic", "drone", "uav", "air defence", "air defense"],
    "Military": ["military", "air strike", "airstrike", "war", "attack", "defence", "defense", "navy"],
    "Hormuz & Maritime": ["hormuz", "shipping", "maritime", "tanker", "naval", "gulf of oman", "blockade"],
    "Energy": ["oil", "gas", "energy", "opec", "lng", "petroleum", "refinery"],
    "Domestic Politics": ["khamenei", "pezeshkian", "parliament", "election", "regime", "government", "succession"],
    "Protests & Rights": ["protest", "human rights", "execution", "dissident", "prison", "women"],
    "Regional Influence": ["iraq", "syria", "lebanon", "hezbollah", "houthi", "yemen", "proxy"],
    "Russia & China": ["russia", "russian", "china", "chinese", "moscow", "beijing"],
    "Diplomacy": ["diplomacy", "talks", "negotiation", "agreement", "deal", "ceasefire", "mediation"],
    "Economy": ["economy", "economic", "inflation", "currency", "rial", "trade", "banking"],
    "Cyber": ["cyber", "hack", "malware", "digital attack"],
}

HIGH_IMPORTANCE = [
    "nuclear weapon", "iaea", "fordow", "natanz", "enrichment", "uranium",
    "strait of hormuz", "war", "air strike", "airstrike", "missile attack",
    "ceasefire", "sanctions", "revolutionary guard", "irgc", "security council"
]
MEDIUM_IMPORTANCE = [
    "trump", "khamenei", "pezeshkian", "israel", "united states", "oil",
    "diplomacy", "negotiation", "proxy", "hezbollah", "houthi"
]
ANALYSIS_HINTS = ["analysis", "opinion", "commentary", "explainer", "investigation", "feature", "long read", "editorial"]


def now() -> datetime:
    return datetime.now(timezone.utc)


def clean(value: str | None) -> str:
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = dateparser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def classify_topics(text: str) -> list[str]:
    lower = text.lower()
    tags = [topic for topic, terms in TOPICS.items() if any(term in lower for term in terms)]
    return tags[:7] or ["General Iran"]


def content_kind(source: dict, title: str, url: str, text: str) -> str:
    if source.get("type") == "official":
        return "Official statement"
    lower = f"{title} {url} {text}".lower()
    if any(term in lower for term in ANALYSIS_HINTS):
        return "Analysis"
    if source.get("type") == "think_tank":
        return "Research & analysis"
    return "News & reporting"


def importance(text: str, tags: list[str], source: dict) -> dict:
    lower = text.lower()
    high_hits = sum(1 for term in HIGH_IMPORTANCE if term in lower)
    medium_hits = sum(1 for term in MEDIUM_IMPORTANCE if term in lower)
    source_priority = int(source.get("priority", 3))
    raw = 1 + min(2, high_hits) + (1 if medium_hits >= 2 else 0) + (1 if source_priority >= 5 else 0)
    score = max(1, min(5, raw))
    labels = {5:"Must read",4:"Important",3:"Useful background",2:"Worth a look",1:"Minor mention"}
    return {"score": score, "label": labels[score]}


async def fetch(session: aiohttp.ClientSession, url: str) -> tuple[str, str, str]:
    try:
        async with session.get(url, timeout=25, allow_redirects=True) as response:
            if response.status >= 400:
                return "", "", url
            return await response.text(errors="ignore"), response.headers.get("content-type", ""), str(response.url)
    except Exception:
        return "", "", url


async def discover_feeds(session: aiohttp.ClientSession, source: dict) -> list[str]:
    homepage = source["url"]
    found: set[str] = set(source.get("feeds", []))
    html, _, final_url = await fetch(session, homepage)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("link", href=True):
            rel = " ".join(tag.get("rel", [])).lower()
            typ = (tag.get("type") or "").lower()
            if "alternate" in rel and ("rss" in typ or "atom" in typ or "xml" in typ):
                found.add(urljoin(final_url, tag["href"]))
    found.update(urljoin(final_url, path) for path in FEED_PATHS)
    return list(found)


def make_item(source: dict, title: str, url: str, published: datetime, summary: str, raw_text: str) -> dict | None:
    text = f"{title}\n{summary}\n{raw_text}"
    matches = sorted({m.group(0) for m in IRAN_RE.finditer(text)}, key=str.lower)
    if not matches:
        return None
    tags = classify_topics(text)
    rank = importance(text, tags, source)
    return {
        "source": source["name"],
        "source_type": source.get("type", "other"),
        "country": source.get("country", "International"),
        "source_priority": source.get("priority", 3),
        "content_kind": content_kind(source, title, url, text),
        "title": title,
        "url": url,
        "published_utc": published.isoformat(),
        "summary": summary[:1100],
        "matched_terms": matches,
        "tags": tags,
        "importance_score": rank["score"],
        "importance_label": rank["label"],
    }


async def scan_feed(session, source: dict, feed_url: str, cutoff: datetime) -> list[dict]:
    raw, content_type, _ = await fetch(session, feed_url)
    head = raw[:900].lower()
    if not raw or ("<rss" not in head and "<feed" not in head and "xml" not in content_type.lower()):
        return []
    parsed = feedparser.parse(raw)
    results = []
    for entry in parsed.entries:
        title = clean(entry.get("title"))
        summary = clean(entry.get("summary") or entry.get("description") or entry.get("content", [{}])[0].get("value", ""))
        published = parse_date(entry.get("published") or entry.get("updated") or entry.get("created"))
        url = entry.get("link", "")
        if not title or not url or not published or published < cutoff or published > now() + timedelta(hours=3):
            continue
        item = make_item(source, title, url, published, summary, summary)
        if item:
            results.append(item)
    return results


async def discover_sitemap_urls(session: aiohttp.ClientSession, source: dict, cutoff: datetime) -> list[tuple[str, datetime | None]]:
    base = source["url"]
    candidates = source.get("sitemaps", []) or [urljoin(base, "/sitemap.xml"), urljoin(base, "/sitemap_index.xml"), urljoin(base, "/news-sitemap.xml")]
    collected: list[tuple[str, datetime | None]] = []
    seen = set()
    for sitemap_url in candidates[:3]:
        raw, _, _ = await fetch(session, sitemap_url)
        if not raw or "<loc>" not in raw:
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue
        locs = []
        for node in root.iter():
            if node.tag.endswith("loc") and node.text:
                locs.append(node.text.strip())
        # Follow a small number of recent child sitemaps.
        if root.tag.endswith("sitemapindex"):
            for child in locs[:5]:
                child_raw, _, _ = await fetch(session, child)
                if not child_raw:
                    continue
                try: child_root = ET.fromstring(child_raw)
                except ET.ParseError: continue
                for url_node in child_root.iter():
                    if url_node.tag.endswith("url"):
                        loc = next((n.text.strip() for n in url_node if n.tag.endswith("loc") and n.text), None)
                        modified = next((parse_date(n.text) for n in url_node if n.tag.endswith("lastmod") and n.text), None)
                        if loc and (not modified or modified >= cutoff) and loc not in seen:
                            seen.add(loc); collected.append((loc, modified))
                            if len(collected) >= MAX_SITEMAP_URLS: return collected
        else:
            for url_node in root.iter():
                if url_node.tag.endswith("url"):
                    loc = next((n.text.strip() for n in url_node if n.tag.endswith("loc") and n.text), None)
                    modified = next((parse_date(n.text) for n in url_node if n.tag.endswith("lastmod") and n.text), None)
                    if loc and (not modified or modified >= cutoff) and loc not in seen:
                        seen.add(loc); collected.append((loc, modified))
                        if len(collected) >= MAX_SITEMAP_URLS: return collected
    return collected


async def scan_article_page(session: aiohttp.ClientSession, source: dict, url: str, fallback_date: datetime | None, cutoff: datetime) -> dict | None:
    raw, _, final_url = await fetch(session, url)
    if not raw:
        return None
    soup = BeautifulSoup(raw, "html.parser")
    title = clean((soup.find("meta", property="og:title") or {}).get("content") if soup.find("meta", property="og:title") else "")
    if not title and soup.title:
        title = clean(soup.title.get_text())
    desc_tag = soup.find("meta", attrs={"name":"description"}) or soup.find("meta", property="og:description")
    summary = clean(desc_tag.get("content") if desc_tag else "")
    date_values = []
    for attrs in [
        {"property":"article:published_time"}, {"name":"date"}, {"name":"pubdate"},
        {"itemprop":"datePublished"}, {"name":"parsely-pub-date"}
    ]:
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"): date_values.append(tag.get("content"))
    time_tag = soup.find("time", datetime=True)
    if time_tag: date_values.append(time_tag.get("datetime"))
    published = next((dt for dt in (parse_date(v) for v in date_values) if dt), fallback_date)
    if not published or published < cutoff or published > now() + timedelta(hours=3):
        return None
    body_text = clean(" ".join(p.get_text(" ", strip=True) for p in soup.find_all("p")[:20]))
    return make_item(source, title, final_url, published, summary, body_text)


async def scan_source(session, source: dict, cutoff: datetime) -> tuple[list[dict], dict]:
    feeds = await discover_feeds(session, source)
    groups = await asyncio.gather(*(scan_feed(session, source, feed_url, cutoff) for feed_url in feeds), return_exceptions=True)
    items = [item for group in groups if isinstance(group, list) for item in group]
    method = "feed" if items else "none"
    if not items:
        sitemap_urls = await discover_sitemap_urls(session, source, cutoff)
        page_results = await asyncio.gather(*(
            scan_article_page(session, source, url, modified, cutoff)
            for url, modified in sitemap_urls[:MAX_ARTICLE_FETCHES_PER_SOURCE]
        ), return_exceptions=True)
        items = [item for item in page_results if isinstance(item, dict)]
        if sitemap_urls: method = "sitemap"
    return items, {"source":source["name"], "method":method, "items":len(items)}


def load_existing() -> list[dict]:
    if not OUTPUT.exists(): return []
    try: return json.loads(OUTPUT.read_text(encoding="utf-8")).get("items", [])
    except Exception: return []


async def main() -> None:
    scan_cutoff = now() - timedelta(hours=SCAN_HOURS)
    archive_cutoff = now() - timedelta(days=ARCHIVE_DAYS)
    headers = {"User-Agent": "IranIntelligenceMonitor/3.1 (journalistic research monitor; contact via repository)"}
    connector = aiohttp.TCPConnector(limit=20)
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(headers=headers, connector=connector, timeout=timeout) as session:
        groups = await asyncio.gather(*(scan_source(session, source, scan_cutoff) for source in SOURCES), return_exceptions=True)

    unique: dict[str, dict] = {}
    for item in load_existing():
        published = parse_date(item.get("published_utc"))
        if published and published >= archive_cutoff:
            key = item.get("url", "").rstrip("/") or item.get("source", "") + "|" + item.get("title", "").lower()
            unique[key] = item

    source_status=[]
    for group in groups:
        if isinstance(group, tuple):
            items, status = group
            source_status.append(status)
            for item in items:
                key = item["url"].rstrip("/") or item["source"] + "|" + item["title"].lower()
                unique[key] = item

    # Store the archive newest-first. The website can still re-sort it by importance, source or oldest-first.
    items = sorted(unique.values(), key=lambda i: i.get("published_utc", ""), reverse=True)
    type_counts={}
    country_counts={}
    for item in items:
        type_counts[item.get("source_type","other")]=type_counts.get(item.get("source_type","other"),0)+1
        country_counts[item.get("country","International")]=country_counts.get(item.get("country","International"),0)+1
    payload = {
        "generated_utc": now().isoformat(), "scan_hours":SCAN_HOURS, "archive_days":ARCHIVE_DAYS,
        "source_count":len(SOURCES), "item_count":len(items), "type_counts":type_counts,
        "country_counts":country_counts, "source_status":source_status, "items":items
    }
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(items)} items from {len(SOURCES)} sources to {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
