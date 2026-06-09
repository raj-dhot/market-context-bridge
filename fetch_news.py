import datetime as dt
import html
import json
import re
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import requests
import trafilatura
from bs4 import BeautifulSoup
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
    "Competitor & AI Pulse": "Wealthsimple OR Questrade OR AI wealth management",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

BING_NEWS_RSS_BASE = "https://www.bing.com/news/search?q={query}&format=rss&count=20"

REQUEST_TIMEOUT = 20

# Rolling-month window: the script runs on the first Monday of each month
# and reviews everything published in the preceding LOOKBACK_DAYS days.
LOOKBACK_DAYS = 31

# More articles per category now that the window covers a whole month.
NEWS_ITEM_TARGET = 5
NEWS_BODY_MIN_LENGTH = 350
NEWS_BODY_MAX_CHARS = 2000

# Podcasts: collect every qualifying episode from the past month,
# capped per show so the report stays a manageable size.
# Tiered detail: the MOST RECENT episode of each show gets the full
# transcript (huge cap); older episodes in the window get a short
# summary from show notes / the feed description only.
MAX_EPISODES_PER_SHOW = 6
LATEST_TRANSCRIPT_MAX_CHARS = 100_000   # effectively "full" for a ~90 min show
OLDER_EPISODE_MAX_CHARS = 1_500         # show-notes summary for older episodes

OUTPUT_FILE = Path("latest_news.txt")
YOUTUBE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_SHORTS_PATTERN = re.compile(r"youtube\.com/shorts/", re.IGNORECASE)

# Delay between outbound fetches (be polite)
NEWS_FETCH_DELAY = 2
EPISODE_FETCH_DELAY = 2

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

    # Zero-width / invisible unicode chars
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


def is_recent(value, days=LOOKBACK_DAYS):
    published_at = parse_datetime(value)
    if published_at is None:
        return True
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=dt.timezone.utc)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    return published_at >= cutoff


def lookback_window_label(days=LOOKBACK_DAYS):
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=days)
    return f"{start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}"


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


def find_all_tags(parent, *names):
    wanted = {name.split(":")[-1].lower() for name in names}
    matches = []
    for tag in parent.find_all():
        tag_name = getattr(tag, "name", None)
        if tag_name and tag_name.split(":")[-1].lower() in wanted:
            matches.append(tag)
    return matches


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
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        extracted = trafilatura.extract(
            downloaded, include_comments=False, include_links=False
        )
        cleaned = normalize_whitespace(extracted)
        return cleaned or None
    except Exception:
        return None


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


# ── BING NEWS RSS ─────────────────────────────────────────────────────────────


def fetch_bing_news_rss(query, session, max_items=20):
    """Fetch news items from Bing News RSS for the given query.

    Bing returns direct article URLs (no encoding or redirect tricks),
    making downstream extraction with trafilatura straightforward.

    Returns a list of dicts with keys: title, url, source, published.
    """
    feed_url = BING_NEWS_RSS_BASE.format(query=quote(query))
    print(f"  [Bing News] Fetching: {feed_url}")
    try:
        resp = session.get(feed_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        print(f"  [Bing News] RSS fetch failed for '{query}': {exc}")
        return []

    soup = BeautifulSoup(resp.content, "xml")
    items = soup.find_all("item")
    print(f"  [Bing News] Found {len(items)} items for '{query}'")
    results = []

    for item in items[:max_items]:
        title = extract_tag_text(item, "title") or "Untitled"
        link = extract_tag_text(item, "link")
        pub_date = extract_tag_text(item, "pubDate")
        source_tag = find_first_tag(item, "news:source", "source")
        source = (
            normalize_whitespace(source_tag.get_text(" ", strip=True))
            if source_tag
            else "Unknown"
        )

        if not link:
            continue

        results.append({
            "title": title,
            "url": link,
            "source": source,
            "published": pub_date,
        })

    # Newest first so the monthly digest leads with fresh coverage but
    # still reaches back across the full window.
    results.sort(
        key=lambda r: parse_datetime(r["published"])
        or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        reverse=True,
    )
    return results


# ── NEWS SECTION ──────────────────────────────────────────────────────────────


def build_news_section():
    lines = []
    query_list = list(NEWS_QUERIES.items())

    with requests.Session() as session:
        session.headers.update(HEADERS)

        for idx, (category, query) in enumerate(query_list):
            lines.append(f"### {category.upper()} ###")
            added = 0
            seen_urls = set()

            try:
                results = fetch_bing_news_rss(query, session)
            except Exception as exc:
                lines.append(f"News fetch error: {exc}")
                lines.append("")
                if idx < len(query_list) - 1:
                    time.sleep(NEWS_FETCH_DELAY)
                continue

            if not results:
                lines.append("No results returned from Bing News RSS.")
                lines.append("")
                if idx < len(query_list) - 1:
                    time.sleep(NEWS_FETCH_DELAY)
                continue

            for result in results:
                if added >= NEWS_ITEM_TARGET:
                    break

                url = result["url"]
                title = result["title"]
                published = result["published"]
                source = result["source"]

                if published and not is_recent(published):
                    print(f"  [Skip] Outside monthly window: {title[:60]}")
                    continue

                if url in seen_urls:
                    continue
                seen_urls.add(url)
                print(f"  [Fetching] {url[:80]}")

                # Fetch article body via trafilatura
                body = fetch_article_text(url)
                body_len = len(body) if body else 0
                if not body or body_len < NEWS_BODY_MIN_LENGTH:
                    print(
                        f"  [Skip] Body too short ({body_len} chars): "
                        f"{title[:60]}"
                    )
                    continue

                if not source or source == "Unknown":
                    source = source_name_from_url(url)

                lines.extend([
                    f"TITLE: {title}",
                    f"SOURCE: {source}",
                    f"PUBLISHED: {published or 'Unknown'}",
                    f"URL: {url}",
                    f"CONTENT: {truncate_text(body, NEWS_BODY_MAX_CHARS)}",
                    "",
                ])
                added += 1

            if added == 0 and not any(
                "News fetch error" in l for l in lines[-3:]
            ):
                lines.append(
                    "No recent articles met the extraction threshold."
                )
                lines.append("")

            # Polite delay between category fetches
            if idx < len(query_list) - 1:
                time.sleep(NEWS_FETCH_DELAY)

    return "\n".join(lines)


# ── PODCAST SECTION ───────────────────────────────────────────────────────────

SRT_VTT_TIMESTAMP = re.compile(
    r"^\s*(?:\d+\s*$|(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s*-->.*$|WEBVTT.*$"
    r"|NOTE\b.*$|STYLE\b.*$|REGION\b.*$)",
    re.MULTILINE,
)
VTT_INLINE_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def parse_transcript_payload(text, mime_type):
    """Convert a Podcasting 2.0 transcript payload to plain text.

    Handles text/plain, text/html, SRT, VTT, and the JSON segment format.
    """
    mime = (mime_type or "").lower()
    stripped = (text or "").lstrip()

    if "json" in mime or stripped.startswith(("{", "[")):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                segments = data.get("segments") or []
            elif isinstance(data, list):
                segments = data
            else:
                segments = []
            parts = [
                seg.get("body", "")
                for seg in segments
                if isinstance(seg, dict) and seg.get("body")
            ]
            if parts:
                return " ".join(parts)
        except (ValueError, TypeError):
            pass  # fall through and treat as text

    if "html" in mime or stripped.startswith("<"):
        return html_to_text(text)

    # SRT / VTT / plain text: strip cue numbers, timestamps, headers, tags
    cleaned = SRT_VTT_TIMESTAMP.sub("", text or "")
    cleaned = VTT_INLINE_TAG.sub("", cleaned)
    return cleaned


def fetch_podcast_transcript(item, session):
    """Fetch a full transcript via the <podcast:transcript> tag, if present.

    Prefers plain text, then JSON, then SRT/VTT, then HTML. Returns
    cleaned transcript text or None.
    """
    tags = find_all_tags(item, "podcast:transcript", "transcript")
    if not tags:
        return None

    type_preference = {
        "text/plain": 0,
        "application/json": 1,
        "application/srt": 2,
        "application/x-subrip": 2,
        "text/vtt": 2,
        "text/html": 3,
    }
    tags.sort(
        key=lambda t: type_preference.get((t.get("type") or "").lower(), 4)
    )

    for tag in tags:
        url = tag.get("url")
        if not url:
            continue
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except Exception:
            continue
        text = parse_transcript_payload(resp.text, tag.get("type"))
        cleaned = clean_social_noise(text)
        # A real transcript should be substantial; tiny payloads are
        # usually errors or placeholder files.
        if cleaned and len(cleaned) > 500:
            return cleaned

    return None


def parse_youtube_episode(entry, want_full_transcript):
    """Turn a YouTube Atom <entry> into an episode dict.

    Latest episode (want_full_transcript=True): fetch the complete
    transcript. Older episodes: description summary only (fast).
    """
    title = extract_tag_text(entry, "title") or "Unknown episode"
    link = extract_item_link(entry) or "Unavailable"
    published = extract_tag_text(entry, "published", "updated") or "Unknown"

    transcript = None
    if want_full_transcript:
        transcript = fetch_youtube_transcript(link)

    if transcript:
        data_type = "Full transcript"
        content = transcript
        max_chars = LATEST_TRANSCRIPT_MAX_CHARS
    else:
        desc = find_first_tag(
            entry, "media:description", "description", "summary"
        )
        raw = desc.get_text(" ", strip=True) if desc else ""
        content = (
            clean_social_noise(raw)
            or "No transcript or description available."
        )
        data_type = (
            "Show notes (transcript unavailable)"
            if want_full_transcript
            else "Show notes summary"
        )
        max_chars = (
            LATEST_TRANSCRIPT_MAX_CHARS
            if want_full_transcript
            else OLDER_EPISODE_MAX_CHARS
        )

    return {
        "title": title,
        "published": published,
        "url": link,
        "data_type": data_type,
        "content": content,
        "max_chars": max_chars,
    }


def parse_rss_episode(item, session, want_full_transcript):
    """Turn a standard RSS <item> into an episode dict.

    Latest episode (want_full_transcript=True): try, in order,
      1. <podcast:transcript> tag (Podcasting 2.0 full transcript)
      2. episode web page (often contains a posted transcript)
      3. show notes from the feed
    Older episodes: show notes summary only (fast).
    """
    title = extract_tag_text(item, "title") or "Unknown episode"
    link = extract_item_link(item) or "Unavailable"
    published = extract_tag_text(
        item, "published", "updated", "pubDate", "dc:date"
    ) or "Unknown"

    if not want_full_transcript:
        return {
            "title": title,
            "published": published,
            "url": link,
            "data_type": "Show notes summary",
            "content": extract_episode_notes(item),
            "max_chars": OLDER_EPISODE_MAX_CHARS,
        }

    # 1. Podcasting 2.0 transcript tag
    transcript = fetch_podcast_transcript(item, session)
    if transcript:
        return {
            "title": title,
            "published": published,
            "url": link,
            "data_type": "Full transcript (podcast:transcript)",
            "content": transcript,
            "max_chars": LATEST_TRANSCRIPT_MAX_CHARS,
        }

    # 2. Episode page (some shows publish the transcript there)
    page_content = None
    if link and link.startswith("http") and not link.endswith(".mp3"):
        page_content = fetch_article_text(link)
        if page_content:
            page_content = clean_social_noise(page_content)

    if page_content and len(page_content) > 200:
        return {
            "title": title,
            "published": published,
            "url": link,
            "data_type": "Episode page (may include transcript)",
            "content": page_content,
            "max_chars": LATEST_TRANSCRIPT_MAX_CHARS,
        }

    # 3. Show notes fallback
    return {
        "title": title,
        "published": published,
        "url": link,
        "data_type": "Show notes (transcript unavailable)",
        "content": extract_episode_notes(item),
        "max_chars": LATEST_TRANSCRIPT_MAX_CHARS,
    }


def fetch_youtube_episodes(show_name, feed_url, session):
    """Fetch ALL full episodes from the past month from a YouTube feed.

    Skips Shorts, filters to the rolling lookback window, and caps the
    count at MAX_EPISODES_PER_SHOW. Returns a (possibly empty) list.
    """
    response = session.get(feed_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "xml")

    entries = soup.find_all("entry")
    if not entries:
        raise ValueError("Feed contained no <entry> nodes")

    episodes = []
    for entry in entries:
        if len(episodes) >= MAX_EPISODES_PER_SHOW:
            break
        if is_youtube_short(entry):
            continue
        published = extract_tag_text(entry, "published", "updated")
        if published and not is_recent(published):
            continue
        # Feeds are newest-first: the first episode we keep is the most
        # recent one and gets the full transcript; the rest get summaries.
        want_full = len(episodes) == 0
        print(
            f"  [{show_name}] Parsing episode published {published} "
            f"({'full transcript' if want_full else 'summary'})"
        )
        episodes.append(parse_youtube_episode(entry, want_full))
        time.sleep(EPISODE_FETCH_DELAY)

    return episodes


def fetch_rss_episodes(show_name, feed_url, session):
    """Fetch ALL episodes from the past month from a standard RSS feed.

    Filters to the rolling lookback window and caps the count at
    MAX_EPISODES_PER_SHOW. Returns a (possibly empty) list.
    """
    response = session.get(feed_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "xml")

    items = soup.find_all("item")
    if not items:
        raise ValueError("Feed contained no <item> nodes")

    episodes = []
    for item in items:
        if len(episodes) >= MAX_EPISODES_PER_SHOW:
            break
        published = extract_tag_text(
            item, "published", "updated", "pubDate", "dc:date"
        )
        if published and not is_recent(published):
            # Standard podcast RSS feeds are newest-first; once we hit an
            # episode older than the window, everything after it is too.
            break
        # First kept episode = most recent = full transcript attempt.
        want_full = len(episodes) == 0
        print(
            f"  [{show_name}] Parsing episode published {published} "
            f"({'full transcript' if want_full else 'summary'})"
        )
        episodes.append(parse_rss_episode(item, session, want_full))
        time.sleep(EPISODE_FETCH_DELAY)

    return episodes


def build_podcast_section(session):
    lines = [
        "### SOCIAL & PODCAST INTELLIGENCE "
        f"(ALL EPISODES, {lookback_window_label()}) ###"
    ]

    for show_name, feeds in PODCAST_FEEDS.items():
        episodes = []
        errors = []

        if "youtube" in feeds:
            try:
                episodes = fetch_youtube_episodes(
                    show_name, feeds["youtube"], session
                )
            except Exception as exc:
                errors.append(f"YouTube: {exc}")

        if not episodes and "rss" in feeds:
            try:
                episodes = fetch_rss_episodes(
                    show_name, feeds["rss"], session
                )
            except Exception as exc:
                errors.append(f"RSS: {exc}")

        if episodes:
            lines.append(
                f"SHOW: {show_name} "
                f"({len(episodes)} episode(s) in the past month; "
                "latest = full transcript, older = summary)"
            )
            lines.append("")
            for episode in episodes:
                lines.extend([
                    f"EPISODE: {episode['title']}",
                    f"PUBLISHED: {episode['published']}",
                    f"URL: {episode['url']}",
                    f"DATA_TYPE: {episode['data_type']}",
                    "DATA:",
                    truncate_text(
                        episode["content"], episode["max_chars"]
                    ),
                    "-" * 50,
                    "",
                ])
        elif errors:
            lines.extend([
                f"SHOW: {show_name}",
                f"ERROR: {'; '.join(errors)}",
                "-" * 50,
                "",
            ])
        else:
            lines.extend([
                f"SHOW: {show_name}",
                "No episodes published in the past month.",
                "-" * 50,
                "",
            ])

    return "\n".join(lines)


# ── MAIN ──────────────────────────────────────────────────────────────────────


def build_report():
    today = dt.datetime.now().strftime("%Y-%m-%d")
    sections = [
        f"OCEANFRONT MARKET INTELLIGENCE - MONTHLY REVIEW - {today}",
        f"COVERAGE WINDOW: {lookback_window_label()}",
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
