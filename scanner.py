#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import time
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
ARCHIVE_DAYS = 10
MAX_SITEMAP_URLS = 35
MAX_ARTICLE_FETCHES_PER_SOURCE = 12

PATTERNS = [
    # English and Latin-script variants
    r"\biran(?:ian|ians)?\b", r"\biranien(?:ne|nes|s)?\b", r"\biranisch\w*\b",
    r"\biranian[oa]s?\b", r"\biran[ií]\w*\b", r"\biranlı\w*\b", r"\biran\b",
    r"\bteh[eé]ran\b", r"\bteheran\b", r"\bteherán\b", r"\btahran\b",
    # Cyrillic, Hebrew, Chinese, Japanese, Korean and Greek
    r"иран\w*", r"тегеран\w*", r"איראן", r"איראני\w*", r"טהרן",
    r"伊朗", r"德黑兰", r"イラン", r"テヘラン", r"이란", r"테헤란", r"Ιράν", r"Τεχεράνη",
    # Important Iran-specific entities that may appear without the country name
    r"\birgc\b", r"\brevolutionary guards?\b", r"\bkhamenei\b", r"\bpezeshkian\b",
    r"\bnatanz\b", r"\bfordow\b", r"\bquds force\b", r"\bbasij\b",
    r"سپاه پاسداران", r"خامنه[‌ ]?ای", r"نتنز", r"فردو", r"משמרות המהפכה",
    r"хамене[ия]", r"ксир", r"哈梅内伊", r"革命卫队", r"ハメネイ", r"혁명수비대"
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
ANALYSIS_TITLE_HINTS = [
    "analysis", "opinion", "commentary", "explainer", "investigation", "feature",
    "long read", "editorial", "perspective", "assessment", "policy brief",
    "policy paper", "working paper", "research paper", "expert view", "expert analysis",
    "what it means", "what next", "what does", "why ", "how ",
    "背景", "分析", "解説", "观点", "комментарий", "анализ", "analyse",
    "commentaire", "opinion", "opinión", "análisis", "analisi", "yorum", "ניתוח", "פרשנות"
]
ANALYSIS_URL_MARKERS = [
    "/analysis/", "/opinion/", "/commentary/", "/editorial/", "/features/",
    "/feature/", "/investigations/", "/investigation/", "/explainers/",
    "/explainer/", "/research/", "/publications/", "/policy-brief", "/long-read"
]
NEWS_EVENT_HINTS = [
    "suspends", "suspended", "attacks", "attacked", "strikes", "struck", "announces",
    "announced", "confirms", "confirmed", "says", "said", "warns", "warned", "kills",
    "killed", "signs", "signed", "meets", "met", "agrees", "agreed", "launches",
    "launched", "orders", "ordered", "approves", "approved", "rejects", "rejected",
    "resumes", "resumed", "halts", "halted", "closes", "closed", "opens", "opened",
    "releases", "released", "detains", "detained", "arrests", "arrested", "votes",
    "voted", "wins", "won", "loses", "lost", "hits", "hit", "reports", "reported"
]
NEWS_URL_MARKERS = ["/news/", "/live/", "/breaking/", "/latest/"]



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


def classify_content(source: dict, title: str, url: str, text: str) -> tuple[str, str]:
    """Classify content conservatively using title and URL signals.

    News and regional outlets default to news. Their summaries/body text are deliberately
    excluded from analysis detection because words such as "report", "agreement" or
    "assessment" often occur inside ordinary news copy and caused false positives.
    """
    source_type = source.get("type", "other")
    title_lower = (title or "").lower().strip()
    url_lower = (url or "").lower()

    if source_type == "official":
        return "official", "Official statement"
    if source_type == "think_tank":
        return "analysis", "Research & analysis"

    explicit_analysis = any(marker in url_lower for marker in ANALYSIS_URL_MARKERS) or any(
        hint in title_lower for hint in ANALYSIS_TITLE_HINTS
    )
    explicit_news = any(marker in url_lower for marker in NEWS_URL_MARKERS) or any(
        re.search(rf"\b{re.escape(verb)}\b", title_lower) for verb in NEWS_EVENT_HINTS
    )

    # Explicit section labels such as /analysis/ or a headline beginning "Analysis:"
    # override event verbs. Otherwise an event-style headline remains news.
    if explicit_analysis:
        if any(word in title_lower or word in url_lower for word in ("opinion", "editorial", "commentary")):
            return "analysis", "Opinion & commentary"
        if "investigation" in title_lower or "investigative" in title_lower or "/investigation" in url_lower:
            return "analysis", "Investigation"
        if any(word in title_lower or word in url_lower for word in ("explainer", "what it means", "what does", "why ", "how ")):
            return "analysis", "Explainer & analysis"
        return "analysis", "Analysis"
    if explicit_news:
        return "news", "News & reporting"
    return "news", "News & reporting"


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
    content_group, content_label = classify_content(source, title, url, text)
    return {
        "source": source["name"],
        "source_type": source.get("type", "other"),
        "country": source.get("country", "International"),
        "language": source.get("language", "English"),
        "language_code": source.get("language_code", "en"),
        "source_priority": source.get("priority", 3),
        "ownership": source.get("ownership", "Unknown"),
        "region": source.get("region", "International"),
        "reliability": source.get("reliability", 3),
        "iran_expertise": source.get("iran_expertise", 3),
        "original_reporting": source.get("original_reporting", 2),
        "content_group": content_group,
        "content_kind": content_label,
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


def scan_interval_hours(source: dict) -> int:
    source_type = source.get("type", "other")
    if source_type in {"think_tank", "academic"}:
        return 6 if source_type == "think_tank" else 12
    return 1

def should_scan(source: dict, current: datetime) -> bool:
    interval = max(1, int(source.get("scan_interval_hours", scan_interval_hours(source))))
    return current.hour % interval == 0

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
    state = "active" if items else "quiet"
    return items, {"source":source["name"], "method":method, "items":len(items), "state":state, "checked":True}



STOPWORDS = {"the","a","an","and","or","of","to","in","on","for","with","after","before","from","as","at","by","is","are","was","were","be","this","that","its","iran","iranian","says","said","new","amid","over","into","about","how","why"}

def title_tokens(title: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9'-]{2,}", (title or "").lower())
    return {w for w in words if w not in STOPWORDS}

def cluster_items(items: list[dict]) -> list[dict]:
    clusters: list[dict] = []
    for item in sorted(items, key=lambda x: x.get("published_utc", ""), reverse=True):
        dt = parse_date(item.get("published_utc")) or now()
        tokens = title_tokens(item.get("title", ""))
        tags = set(item.get("tags", []))
        best = None; best_score = 0.0
        for cluster in clusters:
            cdt = parse_date(cluster.get("latest_utc")) or now()
            if abs((cdt-dt).total_seconds()) > 48*3600: continue
            ctokens=set(cluster.get("tokens", [])); ctags=set(cluster.get("tags", []))
            union=tokens|ctokens
            lexical=(len(tokens&ctokens)/len(union)) if union else 0
            topic=(len(tags&ctags)/max(1,len(tags|ctags)))
            named=len({t for t in tokens&ctokens if len(t)>=5})
            score=lexical + 0.18*topic + 0.08*min(named,3)
            if score>best_score and (lexical>=0.22 or named>=3): best,best_score=cluster,score
        if best is None:
            clusters.append({"id":f"story-{len(clusters)+1}","headline":item.get("title",""),"latest_utc":item.get("published_utc"),"earliest_utc":item.get("published_utc"),"tokens":sorted(tokens),"tags":sorted(tags),"items":[item]})
        else:
            best["items"].append(item); best["tokens"]=sorted(set(best["tokens"])|tokens); best["tags"]=sorted(set(best["tags"])|tags)
            if item.get("published_utc","")>best.get("latest_utc",""): best["latest_utc"]=item.get("published_utc"); best["headline"]=item.get("title","")
            if item.get("published_utc","")<best.get("earliest_utc",""): best["earliest_utc"]=item.get("published_utc")
    current=now()
    for c in clusters:
        cis=c["items"]; sources={x.get("source") for x in cis}; languages={x.get("language") for x in cis}; countries={x.get("country") for x in cis}
        recent=sum(1 for x in cis if (current-(parse_date(x.get("published_utc")) or current)).total_seconds()<=6*3600)
        c["source_count"]=len(sources); c["language_count"]=len(languages); c["country_count"]=len(countries); c["recent_6h_count"]=recent
        c["importance_score"]=max((x.get("importance_score",1) for x in cis),default=1)
        c["reliability_score"]=round(sum(x.get("reliability",3) for x in cis)/max(1,len(cis)),1)
        c["confidence_score"]=min(5,1+(1 if len(sources)>=2 else 0)+(1 if len(sources)>=4 else 0)+(1 if len(languages)>=2 else 0)+(1 if any(x.get("source_type")=="official" for x in cis) else 0))
        c["emerging_score"]=recent*2+len(sources)+len(languages)
        c["is_emerging"]=recent>=2 and len(sources)>=2
        c["is_outlier"]=len(sources)==1 and max((x.get("reliability",3) for x in cis),default=3)>=4 and recent>=1
        c["content_counts"]={g:sum(1 for x in cis if x.get("content_group")==g) for g in ("news","analysis","official")}
        c["sources"]=sorted(sources); c["languages"]=sorted(languages); c["countries"]=sorted(countries)
        c.pop("tokens",None)
    return sorted(clusters,key=lambda c:(c["is_emerging"],c["emerging_score"],c["latest_utc"]),reverse=True)


def load_existing() -> list[dict]:
    if not OUTPUT.exists(): return []
    try: return json.loads(OUTPUT.read_text(encoding="utf-8")).get("items", [])
    except Exception: return []


async def main() -> None:
    started = time.monotonic()
    current = now()
    scan_cutoff = current - timedelta(hours=SCAN_HOURS)
    archive_cutoff = current - timedelta(days=ARCHIVE_DAYS)
    headers = {"User-Agent": "IranIntelligenceMonitor/5.0 (journalistic research monitor; contact via repository)"}
    connector = aiohttp.TCPConnector(limit=20)
    timeout = aiohttp.ClientTimeout(total=45)
    due_sources = [source for source in SOURCES if should_scan(source, current)]
    skipped_sources = [source for source in SOURCES if source not in due_sources]
    async with aiohttp.ClientSession(headers=headers, connector=connector, timeout=timeout) as session:
        groups = await asyncio.gather(*(scan_source(session, source, scan_cutoff) for source in due_sources), return_exceptions=True)

    unique: dict[str, dict] = {}
    source_lookup = {source["name"]: source for source in SOURCES}
    for item in load_existing():
        published = parse_date(item.get("published_utc"))
        if published and published >= archive_cutoff:
            metadata = source_lookup.get(item.get("source", ""), {})
            item.setdefault("language", metadata.get("language", "English"))
            item.setdefault("language_code", metadata.get("language_code", "en"))
            # Re-run classification for archived items so improvements apply immediately,
            # rather than waiting for each URL to be rediscovered by a feed.
            classification_source = dict(metadata)
            classification_source.setdefault("type", item.get("source_type", "other"))
            item["content_group"], item["content_kind"] = classify_content(
                classification_source,
                item.get("title", ""),
                item.get("url", ""),
                item.get("summary", ""),
            )
            key = item.get("url", "").rstrip("/") or item.get("source", "") + "|" + item.get("title", "").lower()
            unique[key] = item

    previous_items = load_existing()
    previous_keys = {(i.get("url", "").rstrip("/") or i.get("source", "") + "|" + i.get("title", "").lower()) for i in previous_items}
    source_status=[{"source":s["name"], "method":"scheduled", "items":0, "state":"skipped", "checked":False, "next_interval_hours":scan_interval_hours(s)} for s in skipped_sources]
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
    language_counts={}
    content_counts={}
    for item in items:
        type_counts[item.get("source_type","other")]=type_counts.get(item.get("source_type","other"),0)+1
        country_counts[item.get("country","International")]=country_counts.get(item.get("country","International"),0)+1
        language_counts[item.get("language","English")]=language_counts.get(item.get("language","English"),0)+1
        content_counts[item.get("content_group","news")]=content_counts.get(item.get("content_group","news"),0)+1
    clusters = cluster_items(items)
    new_item_count = sum(1 for key in unique if key not in previous_keys)
    runtime_seconds = round(time.monotonic() - started, 1)
    failed_count = sum(1 for g in groups if isinstance(g, Exception))
    payload = {
        "generated_utc": now().isoformat(), "scan_hours":SCAN_HOURS, "archive_days":ARCHIVE_DAYS,
        "source_count":len(SOURCES), "item_count":len(items), "type_counts":type_counts,
        "country_counts":country_counts, "language_counts":language_counts, "content_counts":content_counts,
        "source_status":source_status, "clusters":clusters, "cluster_count":len(clusters),
        "scan_stats":{"checked_sources":len(due_sources),"skipped_sources":len(skipped_sources),"new_items":new_item_count,"failed_sources":failed_count,"runtime_seconds":runtime_seconds},
        "emerging_count":sum(1 for c in clusters if c.get("is_emerging")),
        "outlier_count":sum(1 for c in clusters if c.get("is_outlier")), "items":items
    }
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(items)} items from {len(SOURCES)} sources to {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
