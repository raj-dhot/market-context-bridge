import datetime as dt
import html
import re
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests
import trafilatura
from bs4 import BeautifulSoup

try:
    from duckduckgo_search import DDGS
except ImportError:
    from ddgs import DDGS

from youtube_transcript_api import YouTubeTranscriptApi

PODCAST_FEEDS = {
    "The Loonie Hour (Vancouver/Canada Macro)": "https://www.youtube.com/feeds/videos.xml?channel_id=UCY7S99Xp4I_Vz65_AEPK7Aw",
    "All-In Podcast (Tech/Global Macro)": "https://www.youtube.com/feeds/videos.xml?channel_id=UCESLZhusAkFfsNsApnjF_Cg",
    "Rational Reminder (Canada Investing)": "https://rationalreminder.libsyn.com/rss",
    "The Compound & Friends (US Retail Sentiment)": "https://feeds.megaphone.fm/TCP4771071679",
}

NEWS_QUERIES = {
    "North America (TSX & S&P 500)": "TSX S&P 500 stock market",
    "International & Emerging": "emerging markets international equities",
    "Competitor & AI Pulse": "Wealthsimple OR Questrade OR AI wealth management news",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

REQUEST_TIMEOUT = 20
NEWS_ITEM_TARGET = 2
NEWS_LOOKBACK_DAYS = 7
NEWS_BODY_MIN_LENGTH = 350
NEWS_BODY_MAX_CHARS = 2000
EPISODE_TEXT_MAX_CHARS = 4000
OUTPUT_FILE = Path("latest_news.txt")
YOUTUBE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


def normalize_whitespace(text):
    return re.sub(r"\s+", " ", text or "").strip()


def truncate_text(text, max_chars):
    text = normalize_whitespace(text)
    if len(text) <= max_chars:
        return text
    shortened = text[:max_chars].rsplit(" ", 1)[0]
    return (shortened or text[:max_chars]) + "..."


def html_to_text(fragment):
    if not fragment:
        return ""
    text = BeautifulSoup(html.unescape(fragment), "html.parser").get_text(" ", strip=True)
    return normalize_whitespace(text)


def clean_social_noise(text):
    cleaned = html.unescape(text or "")
    cleaned = re.sub(r"http\S+", " ", cleaned)
    cleaned = re.sub(r"www\.\S+", " ", cleaned)
    cleaned = re.sub(r"\S+@\S+", " ", cleaned)
    cleaned = re.sub(r"[@#]\S+", " ", cleaned)

    for pattern in (
        r"\bFollow(?: us| the besties)?\b[^.!\n]{0,120}",
        r"\bIntro Music Credit\b[^.!\n]{0,120}",
        r"\bIntro Video Credit\b[^.!\n]{0,120}",
    ):
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    return normalize_whitespace(cleaned)


def parse_datetime(value):
    if not value:
        return None

    candidate = value.strip()
    parsers = [
        lambda raw: dt.datetime.fromisoformat(raw.replace("Z", "+00:00")),
        parsedate_to_datetime,
    ]

    for parser in parsers:
        try:
            return parser(candidate)
        except (TypeError, ValueError, IndexError):
            continue

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(candidate, fmt)
        except ValueError:
            continue

    return None


def is_recent(value, days=NEWS_LOOKBACK_DAYS):
    published_at = parse_datetime(value)
    if published_at is None:
        return True
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=dt.timezone.utc)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    return published_at >= cutoff


def source_name_from_url(url):
    if not url:
        return "Unknown"
    hostname = urlparse(url).netloc.lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname or "Unknown"


def find_first_tag(parent, *names):
    wanted = {name.split(":")[-1].lower() for name in names}

    for recursive in (False, True):
        for tag in parent.find_all(recursive=recursive):
            tag_name = getattr(tag, "name", None)
            if tag_name and tag_name.split(":")[-1].lower() in wanted:
                return tag

    return None


def extract_tag_text(parent, *names):
    tag = find_first_tag(parent, *names)
    if not tag:
        return ""
    return normalize_whitespace(tag.get_text(" ", strip=True))


def extract_youtube_video_id(url_or_id):
    if not url_or_id:
        return None

    candidate = url_or_id.strip()
    if candidate.startswith("yt:video:"):
        candidate = candidate.rsplit(":", 1)[-1]

    if YOUTUBE_ID_PATTERN.fullmatch(candidate):
        return candidate

    parsed = urlparse(candidate)
    hostname = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    if "youtu.be" in hostname and path_parts:
        possible = path_parts[0]
        if YOUTUBE_ID_PATTERN.fullmatch(possible):
            return possible

    if "youtube.com" in hostname or "youtube-nocookie.com" in hostname:
        query_id = parse_qs(parsed.query).get("v", [None])[0]
        if query_id and YOUTUBE_ID_PATTERN.fullmatch(query_id):
            return query_id

        if len(path_parts) >= 2 and path_parts[0] in {"embed", "shorts", "live", "v"}:
            possible = path_parts[1]
            if YOUTUBE_ID_PATTERN.fullmatch(possible):
                return possible

    match = re.search(
        r"(?:v=|youtu\.be/|youtube\.com/(?:embed|shorts|live|v)/)([A-Za-z0-9_-]{11})",
        candidate,
    )
    return match.group(1) if match else None


def fetch_youtube_transcript(url_or_id):
    video_id = extract_youtube_video_id(url_or_id)
    if not video_id:
        return None

    chunks = []

    try:
        api = YouTubeTranscriptApi()
        if hasattr(api, "fetch"):
            transcript = api.fetch(video_id)
            for entry in transcript:
                text = getattr(entry, "text", None)
                if text is None and isinstance(entry, dict):
                    text = entry.get("text")
                if text:
                    chunks.append(text)
    except Exception:
        chunks = []

    if not chunks:
        try:
            transcript = YouTubeTranscriptApi.get_transcript(video_id)
            for entry in transcript:
                text = entry.get("text")
                if text:
                    chunks.append(text)
        except Exception:
            return None

    cleaned = clean_social_noise(" ".join(chunks))
    return cleaned or None


def fetch_article_text(url):
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None

    extracted = trafilatura.extract(
        downloaded,
        include_comments=False,
        include_links=False,
    )
    cleaned = normalize_whitespace(extracted)
    return cleaned or None


def get_news_results(ddgs, query):
    try:
        return list(
            ddgs.news(
                query,
                region="wt-wt",
                safesearch="moderate",
                timelimit="w",
                max_results=8,
            )
        )
    except TypeError:
        return list(ddgs.news(query, max_results=8))


def extract_item_link(item):
    for link_tag in item.find_all("link", recursive=False):
        href = link_tag.get("href")
        if href:
            return href.strip()

        text = normalize_whitespace(link_tag.get_text(" ", strip=True))
        if text.startswith("http"):
            return text

    guid_text = extract_tag_text(item, "guid", "id")
    if guid_text:
        if guid_text.startswith("http"):
            return guid_text
        video_id = extract_youtube_video_id(guid_text)
        if video_id:
            return "https://www.youtube.com/watch?v={0}".format(video_id)

    video_id = extract_tag_text(item, "yt:videoId", "videoId")
    if video_id:
        normalized_video_id = extract_youtube_video_id(video_id)
        if normalized_video_id:
            return "https://www.youtube.com/watch?v={0}".format(normalized_video_id)

    enclosure = find_first_tag(item, "enclosure")
    if enclosure and enclosure.get("url"):
        return enclosure["url"].strip()

    return None


def extract_episode_notes(item):
    for tag_names in (
        ("content:encoded", "encoded"),
        ("itunes:summary", "summary"),
        ("description", "media:description"),
    ):
        tag = find_first_tag(item, *tag_names)
        if not tag:
            continue

        raw = tag.decode_contents() or tag.get_text(" ", strip=True)
        text = clean_social_noise(html_to_text(raw))
        if text:
            return text

    return "No transcript or show notes available."


def build_news_section():
    lines = []
    ddgs = DDGS()

    try:
        for category, query in NEWS_QUERIES.items():
            lines.append("### {0} ###".format(category.upper()))
            added = 0
            seen_urls = set()

            try:
                results = get_news_results(ddgs, query)
            except Exception as exc:
                lines.append("News fetch error: {0}".format(exc))
                lines.append("")
                continue

            for result in results:
                if added >= NEWS_ITEM_TARGET:
                    break

                url = normalize_whitespace(result.get("url") or result.get("href"))
                title = normalize_whitespace(result.get("title"))
                published = normalize_whitespace(result.get("date"))

                if not url or url in seen_urls:
                    continue
                if published and not is_recent(published):
                    continue

                seen_urls.add(url)
                body = fetch_article_text(url) or clean_social_noise(result.get("body"))
                if len(body) < NEWS_BODY_MIN_LENGTH:
                    continue

                source = normalize_whitespace(result.get("source")) or source_name_from_url(url)
                lines.extend(
                    [
                        "TITLE: {0}".format(title or "Untitled"),
                        "SOURCE: {0}".format(source),
                        "PUBLISHED: {0}".format(published or "Unknown"),
                        "URL: {0}".format(url),
                        "CONTENT: {0}".format(truncate_text(body, NEWS_BODY_MAX_CHARS)),
                        "",
                    ]
                )
                added += 1

            if added == 0:
                lines.append("No recent articles met the extraction threshold.")
                lines.append("")
    finally:
        close = getattr(ddgs, "close", None)
        if callable(close):
            close()

    return "\n".join(lines)


def build_podcast_section(session):
    lines = ["### SOCIAL & PODCAST INTELLIGENCE ###"]

    for show_name, feed_url in PODCAST_FEEDS.items():
        try:
            response = session.get(feed_url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()

            soup = BeautifulSoup(response.content, "xml")
            item = soup.find("item") or soup.find("entry")
            if item is None:
                raise ValueError("Feed contained no <item> or <entry> nodes")

            title = extract_tag_text(item, "title") or "Unknown episode"
            link = extract_item_link(item) or "Unavailable"
            published = extract_tag_text(
                item, "published", "updated", "pubDate", "dc:date"
            ) or "Unknown"

            transcript = fetch_youtube_transcript(link)
            if transcript:
                data_type = "Transcript"
                content = transcript
            else:
                data_type = "Show notes"
                content = extract_episode_notes(item)

            lines.extend(
                [
                    "SHOW: {0}".format(show_name),
                    "EPISODE: {0}".format(title),
                    "PUBLISHED: {0}".format(published),
                    "URL: {0}".format(link),
                    "DATA_TYPE: {0}".format(data_type),
                    "DATA:",
                    truncate_text(content, EPISODE_TEXT_MAX_CHARS),
                    "-" * 50,
                    "",
                ]
            )
        except Exception as exc:
            lines.extend(
                [
                    "SHOW: {0}".format(show_name),
                    "ERROR: {0}".format(exc),
                    "-" * 50,
                    "",
                ]
            )

    return "\n".join(lines)


def build_report():
    today = dt.datetime.now().strftime("%Y-%m-%d")
    sections = [
        "OCEANFRONT DEEP INTELLIGENCE - {0}".format(today),
        "=" * 50,
        "",
        build_news_section(),
    ]

    with requests.Session() as session:
        session.headers.update(HEADERS)
        sections.append(build_podcast_section(session))

    return "\n".join(section for section in sections if section).strip() + "\n"


def fetch_content(output_file=OUTPUT_FILE):
    report = build_report()
    output_file.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    fetch_content()
