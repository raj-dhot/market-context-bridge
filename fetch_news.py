import datetime as dt
import html
import re
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import trafilatura
from bs4 import BeautifulSoup

try:
    from duckduckgo_search import DDGS
except ImportError:
    from ddgs import DDGS

from youtube_transcript_api import YouTubeTranscriptApi

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

PODCAST_FEEDS = {
    "The Loonie Hour (Vancouver/Canada Macro)": {
        "youtube": "https://www.youtube.com/feeds/videos.xml?channel_id=UCdpU4qvzypmjZbbcLiPWV8A",
        "rss": "https://anchor.fm/s/103db19ac/podcast/rss",
    },
    "All-In Podcast (Tech/Global Macro)": {
        "youtube": "https://www.youtube.com/feeds/videos.xml?channel_id=UCESLZhusAkFfsNsApnjF_Cg",
    },
    "Rational Reminder (Canada Investing)": {
        "rss": "https://rationalreminder.libsyn.com/rss",
    },
    "The Compound & Friends (US Retail Sentiment)": {
        "rss": "https://feeds.megaphone.fm/TCP4771071679",
    },
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

# Regex to detect YouTube Shorts URLs
YOUTUBE_SHORTS_PATTERN = re.compile(r"youtube\.com/shorts/", re.IGNORECASE)

# ── TEXT HELPERS ───────────────────────────────────────────────────────────────


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
    text = BeautifulSoup(
        html.unescape(fragment), "html.parser"
    ).get_text(" ", strip=True)
    return normalize_whitespace(text)


def clean_social_noise(text):
    """Remove URLs, social handles, hashtags, promo filler, disclaimers,
    and zero-width / invisible unicode characters."""
    cleaned = html.unescape(text or "")

    # Zero-width / invisible unicode chars (word joiner, zero-width space, etc.)
    cleaned = re.sub(r"[\u2060\u200b\u200c\u200d\ufeff\u00ad]+", "", cleaned)
    # Common emoji ranges
    cleaned = re.sub(
        r"[\U0001F300-\U0001FAD6\U0001F600-\U0001F64F"
        r"\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF"
        r"\u2600-\u26FF\u2700-\u27BF]+",
        " ",
        cleaned,
    )

    # URLs and emails
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    cleaned = re.sub(r"www\.\S+", " ", cleaned)
    cleaned = re.sub(r"\S+@\S+\.\S+", " ", cleaned)

    # Social handles and hashtags
    cleaned = re.sub(r"[@#]\S+", " ", cleaned)

    # Common promo / filler patterns
    for pattern in (
        r"\bFollow(?:\s+(?:us|the besties|on))?\b[^.!\n]{0,150}",
        r"\bIntro (?:Music|Video) Credit\b[^.!\n]{0,120}",
        r"\bSign up for\b[^.!\n]{0,150}",
        r"\bSubscribe\b[^.!\n]{0,100}",
        r"\bInstagram:\s*\S*",
        r"\bTwitter:\s*\S*",
        r"\bLinkedIn:\s*\S*",
        r"\bTikTok:\s*\S*",
        r"\bGet Your Tickets Here!\s*",
    ):
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    # Legal disclaimers (Compound-style boilerplate)
    cleaned = re.sub(
        r"(?:Public )?Disclosure:.*?(?:adchoices|disclosures)\b.*",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"This (?:podcast|episode) is for informational purposes.*",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"Investing involves (?:the )?risk.*",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"(?:Obviously )?[Nn]othing on this channel should be considered.*",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # HTML entities that survived
    cleaned = re.sub(r"&[a-z]+;", " ", cleaned)
    cleaned = re.sub(r"&#\d+;", " ", cleaned)

    return normalize_whitespace(cleaned)


# ── DATE HELPERS ──────────────────────────────────────────────────────────────


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


# ── XML / RSS HELPERS ─────────────────────────────────────────────────────────


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


# ── YOUTUBE HELPERS ───────────────────────────────────────────────────────────


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
        if len(path_parts) >= 2 and path_parts[0] in {
            "embed", "shorts", "live", "v",
        }:
            possible = path_parts[1]
            if YOUTUBE_ID_PATTERN.fullmatch(possible):
                return possible

    match = re.search(
        r"(?:v=|youtu\.be/|youtube\.com/(?:embed|shorts|live|v)/)"
        r"([A-Za-z0-9_-]{11})",
        candidate,
    )
    return match.group(1) if match else None


def is_youtube_short(entry):
    """Return True if the YouTube feed entry is a Short (not a full episode)."""
    link = extract_item_link(entry)
    if link and YOUTUBE_SHORTS_PATTERN.search(link):
        return True
    return False


def fetch_youtube_transcript(url_or_id):
    video_id = extract_youtube_video_id(url_or_id)
    if not video_id:
        return None

    chunks = []

    # Try the newer .fetch() API first
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

    # Fallback to legacy .get_transcript()
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
        downloaded, include_comments=False, include_links=False
    )
    cleaned = normalize_whitespace(extracted)
    return cleaned or None


# ── RSS ITEM HELPERS ──────────────────────────────────────────────────────────


def extract_item_link(item):
    """Get the best URL from an RSS <item> or Atom <entry>."""
    for link_tag in item.find_all("link", recursive=False):
        href = link_tag.get("href")
        if href:
            return href.strip()
        text = normalize_whitespace(link_tag.get_text(" ", strip=True))
        if text and text.startswith("http"):
            return text

    guid_text = extract_tag_text(item, "guid", "id")
    if guid_text:
        if guid_text.startswith("http"):
            return guid_text
        video_id = extract_youtube_video_id(guid_text)
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"

    video_id = extract_tag_text(item, "yt:videoId", "videoId")
    if video_id:
        normalized = extract_youtube_video_id(video_id)
        if normalized:
            return f"https://www.youtube.com/watch?v={normalized}"

    enclosure = find_first_tag(item, "enclosure")
    if enclosure and enclosure.get("url"):
        return enclosure["url"].strip()

    return None


def extract_episode_notes(item):
    """Pull the richest text from an RSS item's description fields."""
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


# ── NEWS SECTION ──────────────────────────────────────────────────────────────


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


def build_news_section():
    lines = []
    ddgs = DDGS()
    try:
        for category, query in NEWS_QUERIES.items():
            lines.append(f"### {category.upper()} ###")
            added = 0
            seen_urls = set()
            try:
                results = get_news_results(ddgs, query)
            except Exception as exc:
                lines.append(f"News fetch error: {exc}")
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

                body = fetch_article_text(url) or clean_social_noise(
                    result.get("body")
                )
                if not body or len(body) < NEWS_BODY_MIN_LENGTH:
                    continue

                source = normalize_whitespace(
                    result.get("source")
                ) or source_name_from_url(url)
                lines.extend([
                    f"TITLE: {title or 'Untitled'}",
                    f"SOURCE: {source}",
                    f"PUBLISHED: {published or 'Unknown'}",
                    f"URL: {url}",
                    f"CONTENT: {truncate_text(body, NEWS_BODY_MAX_CHARS)}",
                    "",
                ])
                added += 1

            if added == 0:
                lines.append("No recent articles met the extraction threshold.")
                lines.append("")
    finally:
        close = getattr(ddgs, "close", None)
        if callable(close):
            close()

    return "\n".join(lines)


# ── PODCAST SECTION ───────────────────────────────────────────────────────────


def fetch_youtube_episode(show_name, feed_url, session):
    """Fetch the latest *full* episode from a YouTube RSS feed (skip Shorts)."""
    response = session.get(feed_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "xml")

    # Walk entries to find the first non-Short
    entries = soup.find_all("entry")
    if not entries:
        raise ValueError("Feed contained no <entry> nodes")

    chosen = None
    for entry in entries:
        if not is_youtube_short(entry):
            chosen = entry
            break

    if chosen is None:
        # All entries were Shorts — just take the first one
        chosen = entries[0]

    title = extract_tag_text(chosen, "title") or "Unknown episode"
    link = extract_item_link(chosen) or "Unavailable"
    published = extract_tag_text(
        chosen, "published", "updated"
    ) or "Unknown"

    transcript = fetch_youtube_transcript(link)
    if transcript:
        data_type = "Transcript"
        content = transcript
    else:
        desc = find_first_tag(chosen, "media:description", "description", "summary")
        raw = desc.get_text(" ", strip=True) if desc else ""
        content = clean_social_noise(raw) or "No transcript or description available."
        data_type = "Show notes"

    return {
        "title": title,
        "published": published,
        "url": link,
        "data_type": data_type,
        "content": content,
    }


def fetch_rss_episode(show_name, feed_url, session):
    """Fetch the latest episode from a standard RSS feed."""
    response = session.get(feed_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "xml")
    item = soup.find("item")
    if item is None:
        raise ValueError("Feed contained no <item> nodes")

    title = extract_tag_text(item, "title") or "Unknown episode"
    link = extract_item_link(item) or "Unavailable"
    published = extract_tag_text(
        item, "published", "updated", "pubDate", "dc:date"
    ) or "Unknown"

    # For RSS items, try scraping the episode webpage for richer content
    page_content = None
    if link and link.startswith("http") and not link.endswith(".mp3"):
        page_content = fetch_article_text(link)
        if page_content:
            page_content = clean_social_noise(page_content)

    if page_content and len(page_content) > 200:
        data_type = "Episode page"
        content = page_content
    else:
        data_type = "Show notes"
        content = extract_episode_notes(item)

    return {
        "title": title,
        "published": published,
        "url": link,
        "data_type": data_type,
        "content": content,
    }


def build_podcast_section(session):
    lines = ["### SOCIAL & PODCAST INTELLIGENCE ###"]

    for show_name, feeds in PODCAST_FEEDS.items():
        episode = None
        errors = []

        # Try YouTube feed first if available
        if "youtube" in feeds:
            try:
                episode = fetch_youtube_episode(
                    show_name, feeds["youtube"], session
                )
            except Exception as exc:
                errors.append(f"YouTube: {exc}")

        # Try RSS feed as fallback (or primary if no YouTube)
        if episode is None and "rss" in feeds:
            try:
                episode = fetch_rss_episode(show_name, feeds["rss"], session)
            except Exception as exc:
                errors.append(f"RSS: {exc}")

        if episode:
            lines.extend([
                f"SHOW: {show_name}",
                f"EPISODE: {episode['title']}",
                f"PUBLISHED: {episode['published']}",
                f"URL: {episode['url']}",
                f"DATA_TYPE: {episode['data_type']}",
                "DATA:",
                truncate_text(episode["content"], EPISODE_TEXT_MAX_CHARS),
                "-" * 50,
                "",
            ])
        else:
            lines.extend([
                f"SHOW: {show_name}",
                f"ERROR: {'; '.join(errors) or 'Unknown error'}",
                "-" * 50,
                "",
            ])

    return "\n".join(lines)


# ── MAIN ──────────────────────────────────────────────────────────────────────


def build_report():
    today = dt.datetime.now().strftime("%Y-%m-%d")
    sections = [
        f"OCEANFRONT MARKET INTELLIGENCE - {today}",
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
    print(f"Intelligence report written to {output_file}")


if __name__ == "__main__":
    fetch_content()
