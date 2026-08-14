"""Build the public Daily News Brief feed from BusinessLine RSS.

Only already-public article metadata is written to ``news.json``. The script
does not use or publish the private Gmail/LLM credentials from businessline-
brief, and it does not attempt to bypass premium content.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo


BASE = "https://www.thehindubusinessline.com"
SECTIONS = (
    ("money-and-banking", "Money & Banking"),
    ("companies", "Companies"),
    ("economy", "Economy"),
    ("markets", "Markets"),
    ("economy/policy", "Policy"),
)
IST = ZoneInfo("Asia/Kolkata")
OUTPUT = Path(__file__).with_name("news.json")
TIMEOUT = 25
MAX_ARTICLES = 12
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
LIVE_BLOG = re.compile(r"(?:\blive\s*:|\|.*\blive\b|\blive\s+updates?\b)", re.I)


@dataclass
class NewsItem:
    category: str
    date: str
    title: str
    summary: str
    url: str
    image: str
    image_alt: str


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def safe_http_url(value: str) -> str:
    value = clean(value)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return value


def article_url(value: str) -> str:
    value = safe_http_url(value)
    host = urlparse(value).hostname or ""
    if host == "thehindubusinessline.com" or host.endswith(".thehindubusinessline.com"):
        return value
    return ""


def parse_date(raw: str) -> str:
    try:
        return parsedate_to_datetime(raw).astimezone(IST).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return datetime.now(IST).date().isoformat()


def rss_image(item: ET.Element) -> str:
    for node in item.iter():
        local_name = node.tag.rsplit("}", 1)[-1].lower()
        if local_name not in {"content", "thumbnail", "enclosure"}:
            continue
        media_type = (node.attrib.get("type") or "").lower()
        candidate = safe_http_url(node.attrib.get("url", ""))
        if candidate and (not media_type or media_type.startswith("image/")):
            return candidate
    return ""


def page_image(session: requests.Session, url: str) -> str:
    try:
        response = session.get(url, timeout=TIMEOUT)
        response.raise_for_status()
    except requests.RequestException:
        return ""
    soup = BeautifulSoup(response.text, "html.parser")
    for attrs in (
        {"property": "og:image"},
        {"name": "twitter:image"},
        {"name": "twitter:image:src"},
    ):
        tag = soup.find("meta", attrs=attrs)
        candidate = safe_http_url(tag.get("content", "") if tag else "")
        if candidate:
            return candidate
    return ""


def read_feed(session: requests.Session, section: str, category: str) -> list[NewsItem]:
    response = session.get(f"{BASE}/{section}/feeder/default.rss", timeout=TIMEOUT)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    items: list[NewsItem] = []
    for item in root.iter("item"):
        title = clean(item.findtext("title") or "")
        url = article_url(item.findtext("link") or "")
        if not title or not url or LIVE_BLOG.search(title):
            continue
        description_html = item.findtext("description") or ""
        summary = clean(BeautifulSoup(description_html, "html.parser").get_text(" "))
        if not summary:
            continue
        items.append(
            NewsItem(
                category=category,
                date=parse_date(item.findtext("pubDate") or ""),
                title=title,
                summary=summary,
                url=url,
                image=rss_image(item),
                image_alt=f"Article image for {title}",
            )
        )
        if len(items) >= 5:
            break
    return items


def interleave(groups: list[list[NewsItem]]) -> list[NewsItem]:
    result: list[NewsItem] = []
    seen: set[str] = set()
    while any(groups) and len(result) < MAX_ARTICLES:
        for group in groups:
            if not group or len(result) >= MAX_ARTICLES:
                continue
            item = group.pop(0)
            if item.url in seen:
                continue
            seen.add(item.url)
            result.append(item)
    return result


def build() -> dict:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"})
    groups: list[list[NewsItem]] = []
    for section, category in SECTIONS:
        try:
            groups.append(read_feed(session, section, category))
        except (requests.RequestException, ET.ParseError) as exc:
            print(f"warning: {category} feed unavailable: {exc}")
            groups.append([])

    articles = interleave(groups)
    if len(articles) < 4:
        raise RuntimeError(f"refusing to replace the feed: only {len(articles)} articles were found")

    for item in articles:
        if not item.image:
            item.image = page_image(session, item.url)
            time.sleep(0.2)

    return {
        "updated_at": datetime.now(IST).isoformat(timespec="seconds"),
        "source": "The Hindu BusinessLine",
        "articles": [asdict(item) for item in articles],
    }


def main() -> None:
    payload = build()
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(payload['articles'])} articles to {OUTPUT}")


if __name__ == "__main__":
    main()
