#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import re
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
SCAN_HOURS = 48
ARCHIVE_DAYS = 7

PATTERNS = [
    r"\biran(?:ian|ians)?\b", r"\btehran\b", r"\bislamic republic\b",
    r"\birgc\b", r"\brevolutionary guards?\b", r"\bkhamenei\b",
    r"\bpezeshkian\b", r"\bnatanz\b", r"\bfordow\b",
    r"\bstr(?:ait)?\.?\s+of\s+hormuz\b", r"\bquds force\b",
    r"\bbasij\b", r"\biran nuclear\b"
]
IRAN_RE = re.compile("|".join(PATTERNS), re.I)
FEED_PATHS = [
    "/feed/", "/feed", "/rss", "/rss.xml", "/feed.xml",
    "/atom.xml", "/index.xml", "/feeds/all"
]

TOPICS = {
    "Nuclear": ["nuclear", "uranium", "enrichment", "iaea", "fordow", "natanz", "centrifuge"],
    "US–Iran": ["united states", "u.s.", "washington", "trump", "american", "us-iran", "us–iran"],
    "Israel": ["israel", "israeli", "idf", "netanyahu", "tel aviv"],
    "Sanctions": ["sanction", "embargo", "asset freeze", "maximum pressure"],
    "IRGC": ["irgc", "revolutionary guard", "quds force", "basij"],
    "Military": ["missile", "drone", "military", "air strike", "airstrike", "war", "attack", "defence", "defense"],
    "Hormuz & Maritime": ["hormuz", "shipping", "maritime", "tanker", "naval", "gulf of oman"],
    "Energy": ["oil", "gas", "energy", "opec", "lng", "petroleum"],
    "Domestic Politics": ["khamenei", "pezeshkian", "parliament", "election", "regime", "government"],
    "Protests & Rights": ["protest", "human rights", "execution", "dissident", "prison", "women"],
    "Regional Influence": ["iraq", "syria", "lebanon", "hezbollah", "houthi", "yemen", "proxy"],
    "Russia & China": ["russia", "russian", "china", "chinese", "moscow", "beijing"],
    "Diplomacy": ["diplomacy", "talks", "negotiation", "agreement", "deal", "ceasefire", "mediation"],
    "Economy": ["economy", "economic", "inflation", "currency", "rial", "trade"],
}

HIGH_IMPORTANCE = [
    "nuclear weapon", "iaea", "fordow", "natanz", "enrichment", "uranium",
    "strait of hormuz", "war", "air strike", "airstrike", "missile attack",
    "ceasefire", "sanctions", "revolutionary guard", "irgc"
]
MEDIUM_IMPORTANCE = [
    "trump", "khamenei", "pezeshkian", "israel", "united states", "oil",
    "diplomacy", "negotiation", "proxy", "hezbollah", "houthi"
]


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
    return tags[:6] or ["General Iran"]


def importance(text: str, tags: list[str]) -> dict:
    lower = text.lower()
    high_hits = sum(1 for term in HIGH_IMPORTANCE if term in lower)
    medium_hits = sum(1 for term in MEDIUM_IMPORTANCE if term in lower)
    if high_hits >= 2 or (high_hits >= 1 and len(tags) >= 3):
        return {"score": 5, "label": "Must read"}
    if high_hits >= 1:
        return {"score": 4, "label": "Important"}
    if medium_hits >= 2 or len(tags) >= 3:
        return {"score": 3, "label": "Useful background"}
    if medium_hits >= 1:
        return {"score": 2, "label": "Worth a look"}
    return {"score": 1, "label": "Minor mention"}


async def fetch(session: aiohttp.ClientSession, url: str) -> tuple[str, str]:
    try:
        async with session.get(url, timeout=25, allow_redirects=True) as response:
            if response.status >= 400:
                return "", ""
            return await response.text(errors="ignore"), response.headers.get("content-type", "")
    except Exception:
        return "", ""


async def discover_feeds(session: aiohttp.ClientSession, homepage: str) -> list[str]:
    html, _ = await fetch(session, homepage)
    found: set[str] = set()
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("link", href=True):
            rel = " ".join(tag.get("rel", [])).lower()
            typ = (tag.get("type") or "").lower()
            if "alternate" in rel and ("rss" in typ or "atom" in typ or "xml" in typ):
                found.add(urljoin(homepage, tag["href"]))
    found.update(urljoin(homepage, path) for path in FEED_PATHS)
    return list(found)


async def scan_feed(session, source: dict, feed_url: str, cutoff: datetime) -> list[dict]:
    raw, content_type = await fetch(session, feed_url)
    head = raw[:700].lower()
    if not raw or ("<rss" not in head and "<feed" not in head and "xml" not in content_type.lower()):
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

        tags = classify_topics(text)
        rank = importance(text, tags)
        results.append({
            "source": source["name"],
            "title": title,
            "url": url,
            "published_utc": published.isoformat(),
            "summary": summary[:900],
            "matched_terms": matches,
            "tags": tags,
            "importance_score": rank["score"],
            "importance_label": rank["label"],
        })
    return results


async def scan_source(session, source: dict, cutoff: datetime) -> list[dict]:
    feeds = await discover_feeds(session, source["url"])
    groups = await asyncio.gather(
        *(scan_feed(session, source, feed_url, cutoff) for feed_url in feeds),
        return_exceptions=True,
    )
    items = []
    for group in groups:
        if isinstance(group, list):
            items.extend(group)
    return items


def load_existing() -> list[dict]:
    if not OUTPUT.exists():
        return []
    try:
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
        return data.get("items", [])
    except Exception:
        return []


async def main() -> None:
    scan_cutoff = now() - timedelta(hours=SCAN_HOURS)
    archive_cutoff = now() - timedelta(days=ARCHIVE_DAYS)
    headers = {"User-Agent": "IranThinkTankMonitor/2.0 (+public-interest research monitor)"}
    connector = aiohttp.TCPConnector(limit=15)

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        groups = await asyncio.gather(
            *(scan_source(session, source, scan_cutoff) for source in SOURCES),
            return_exceptions=True,
        )

    unique: dict[str, dict] = {}

    for item in load_existing():
        published = parse_date(item.get("published_utc"))
        if published and published >= archive_cutoff:
            key = item.get("url", "").rstrip("/") or item.get("source", "") + "|" + item.get("title", "").lower()
            unique[key] = item

    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            key = item["url"].rstrip("/") or item["source"] + "|" + item["title"].lower()
            unique[key] = item

    items = sorted(
        unique.values(),
        key=lambda item: (item.get("importance_score", 0), item.get("published_utc", "")),
        reverse=True,
    )

    payload = {
        "generated_utc": now().isoformat(),
        "scan_hours": SCAN_HOURS,
        "archive_days": ARCHIVE_DAYS,
        "source_count": len(SOURCES),
        "item_count": len(items),
        "items": items,
    }
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(items)} items to {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
