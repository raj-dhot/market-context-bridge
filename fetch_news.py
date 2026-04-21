import datetime as dt
import html
import os
import re
import sys
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
    # The Compound uses a dedicated pipeline (see fetch_compound_episode).
    "The Compound & Friends (US Retail Sentiment)": {
        "handler": "compound",
        "youtube": "https://www.youtube.com/feeds/videos.xml?channel_id=UCMExRegvFqOy9PSHcnMbsTQ",
        "rss": "https://feeds.megaphone.fm/TCP4771071679",
        "website_search": "https://thecompoundnews.com/?s={query}",
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

BING_NEWS_RSS_BASE = "https://www.bing.com/news/search?q={query}&format=rss&count=10"

REQUEST_TIMEOUT = 20
NEWS_ITEM_TARGET = 2
NEWS_LOOKBACK_DAYS = 7
NEWS_BODY_MIN_LENGTH = 350
NEWS_BODY_MAX_CHARS = 2000
EPISODE_TEXT_MAX_CHARS = 4000
OUTPUT_FILE = Path("latest_news.txt")
YOUTUBE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_SHORTS_PATTERN = re.compile(r"youtube\.com/shorts/", re.IGNORECASE)

# Delay between news RSS fetches (be polite)
NEWS_FETCH_DELAY = 2

# On GitHub Actions (and most cloud/CI IPs), YouTube aggressively blocks
# transcript requests. Detect that context so we can skip the doomed attempt
# and go straight to higher-yield fallbacks. The user can also force-skip
# with SKIP_YOUTUBE_TRANSCRIPTS=1 (or force-try with =0).
_env_override = os.environ.get("SKIP_YOUTUBE_TRANSCRIPTS", "").strip()
if _env_override in ("1", "true", "yes"):
    SKIP_YOUTUBE_TRANSCRIPTS = True
elif _env_override in ("0", "false", "no"):
    SKIP_YOUTUBE_TRANSCRIPTS = False
else:
    SKIP_YOUTUBE_TRANSCRIPTS = bool(
        os.environ.get("GITHUB_ACTIONS")
        or os.environ.get("CI")
    )

# ── LOGGING ───────────────────────────────────────────────────────────────────


def log(msg):
    """Write a diagnostic line to stderr so it's visible in run logs."""
    print(f"[fetch_news] {msg}", file=sys.stderr)


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
    """Fetch a YouTube transcript. Returns (text, error_reason).
    text is None on failure; error_reason is a short string for diagnostics.
    """
    if SKIP_YOUTUBE_TRANSCRIPTS:
        return None, "skipped_ci_env"

    video_id = extract_youtube_video_id(url_or_id)
    if not video_id:
        return None, "invalid_video_id"

    chunks = []
    last_error = None

    try:
        api = YouTubeTranscriptApi()
        transcript = api.fetch(video_id, languages=("en", "en-US", "en-GB"))
        for snippet in transcript:
            text = getattr(snippet, "text", None)
            if text is None and isinstance(snippet, dict):
                text = snippet.get("text")
            if text:
                chunks.append(text)
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {str(exc)[:180]}"

    # Legacy fallback, only if the method still exists in the installed version.
    if not chunks and hasattr(YouTubeTranscriptApi, "get_transcript"):
        try:
            transcript = YouTubeTranscriptApi.get_transcript(video_id)
            for entry in transcript:
                text = entry.get("text") if isinstance(entry, dict) else None
                if text:
                    chunks.append(text)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:180]}"

    if not chunks:
        return None, last_error or "no_snippets"

    cleaned = clean_social_noise(" ".join(chunks))
    if not cleaned:
        return None, "empty_after_cleaning"
    return cleaned, None


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


def fetch_bing_news_rss(query, session, max_items=8):
    """Fetch news items from Bing News RSS for the given query.

    Bing returns direct article URLs (no encoding or redirect tricks),
    making downstream extraction with trafilatura straightforward.

    Returns a list of dicts with keys: title, url, source, published.
    """
    feed_url = BING_NEWS_RSS_BASE.format(query=quote(query))
    log(f"Bing News fetching: {feed_url}")
    try:
        resp = session.get(feed_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        log(f"Bing News RSS fetch failed for '{query}': {exc}")
        return []

    soup = BeautifulSoup(resp.content, "xml")
    items = soup.find_all("item")
    log(f"Bing News found {len(items)} items for '{query}'")
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
                    log(f"Skip (not recent): {title[:60]}")
                    continue

                if url in seen_urls:
                    continue
                seen_urls.add(url)
                log(f"Fetching: {url[:80]}")

                # Fetch article body via trafilatura
                body = fetch_article_text(url)
                body_len = len(body) if body else 0
                if not body or body_len < NEWS_BODY_MIN_LENGTH:
                    log(
                        f"Skip (body too short, {body_len} chars): "
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


def fetch_youtube_episode(show_name, feed_url, session):
    """Fetch the latest *full* episode from a YouTube RSS feed (skip Shorts)."""
    response = session.get(feed_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "xml")

    entries = soup.find_all("entry")
    if not entries:
        raise ValueError("Feed contained no <entry> nodes")

    chosen = None
    for entry in entries:
        if not is_youtube_short(entry):
            chosen = entry
            break

    if chosen is None:
        chosen = entries[0]

    title = extract_tag_text(chosen, "title") or "Unknown episode"
    link = extract_item_link(chosen) or "Unavailable"
    published = extract_tag_text(
        chosen, "published", "updated"
    ) or "Unknown"

    transcript, tr_err = fetch_youtube_transcript(link)
    if transcript:
        data_type = "Transcript"
        content = transcript
    else:
        log(f"[{show_name}] YouTube transcript failed ({tr_err}); using description")
        desc = find_first_tag(
            chosen, "media:description", "description", "summary"
        )
        raw = desc.get_text(" ", strip=True) if desc else ""
        content = (
            clean_social_noise(raw)
            or "No transcript or description available."
        )
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


# ── COMPOUND-SPECIFIC PIPELINE ────────────────────────────────────────────────


def _normalize_title(title):
    """Lowercased, punctuation-stripped title for loose matching."""
    cleaned = re.sub(r"[^\w\s]", " ", (title or "").lower())
    return normalize_whitespace(cleaned)


def _title_similarity(a, b):
    """Jaccard similarity over token sets. 0.0 to 1.0."""
    ta = set(_normalize_title(a).split())
    tb = set(_normalize_title(b).split())
    # Drop very common filler tokens that wouldn't disambiguate.
    stop = {"the", "a", "an", "with", "and", "of", "on", "in", "to", "is",
            "for", "episode", "ep", "ft", "feat"}
    ta -= stop
    tb -= stop
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _find_matching_youtube_entry(entries, rss_title):
    """Given YouTube feed <entry>s and an RSS episode title, return the
    best-matching entry (non-Short), or None."""
    best = None
    best_score = 0.0
    for entry in entries:
        if is_youtube_short(entry):
            continue
        yt_title = extract_tag_text(entry, "title")
        score = _title_similarity(rss_title, yt_title)
        if score > best_score:
            best = entry
            best_score = score
    # Require a reasonable overlap before trusting the match.
    if best_score >= 0.4:
        return best, best_score
    return None, best_score


"""Paths that are category/archive indexes, not individual posts."""
_COMPOUND_ARCHIVE_PATHS = {
    "/", "/home", "/home/",
    "/the-compound-and-friends", "/the-compound-and-friends/",
    "/animal-spirits", "/animal-spirits/",
    "/ask-the-compound", "/ask-the-compound/",
    "/talk-your-book", "/talk-your-book/",
    "/what-are-your-thoughts", "/what-are-your-thoughts/",
    "/talking-wealth", "/talking-wealth/",
    "/podcasts", "/podcasts/",
    "/contact", "/contact/", "/about", "/about/",
}

# Minimum link-text vs episode-title similarity to accept a search hit.
_COMPOUND_ANCHOR_MATCH_THRESHOLD = 0.4


def _looks_like_archive_page(body):
    """Return True if the scraped body text appears to list multiple distinct
    episodes (i.e. it's an archive/index page, not a single post)."""
    if not body:
        return False
    # Count "episode N" or "episode N of" mentions with DIFFERENT numbers.
    matches = re.findall(r"\bepisode\s+(\d{2,4})\b", body.lower())
    distinct_numbers = set(matches)
    if len(distinct_numbers) >= 3:
        return True
    # Count distinct show names showing up as "of X,"
    show_markers = re.findall(
        r"\bof\s+(the compound and friends|animal spirits|ask the compound|"
        r"talking wealth|what are your thoughts|talk your book)\b",
        body.lower(),
    )
    if len(set(show_markers)) >= 3:
        return True
    return False


def _wp_slugify(title):
    """Approximate WordPress's default sanitize_title slug rules."""
    s = (title or "").lower()
    s = s.replace("&", "and")
    # Strip apostrophes entirely (WP removes, not replaces)
    s = re.sub(r"[\u2019']", "", s)
    # Everything else non-alphanumeric becomes a hyphen
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def _try_compound_direct_url(slug, session):
    """Try the direct Compound permalink. Returns (body, url) or None.
    Uses requests.get so we can distinguish 200 from 404.
    """
    url = f"https://thecompoundnews.com/the-compound-and-friends/{slug}/"
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except Exception as exc:
        log(f"[Compound] direct URL request failed ({url}): {exc}")
        return None

    if r.status_code != 200:
        log(f"[Compound] direct URL returned {r.status_code}: {url}")
        return None

    # Extract main article text from the fetched HTML using trafilatura.
    try:
        extracted = trafilatura.extract(
            r.text, include_comments=False, include_links=False
        )
    except Exception as exc:
        log(f"[Compound] trafilatura extract failed: {exc}")
        return None

    body = normalize_whitespace(extracted or "")
    if not body or len(body) < 400:
        log(
            f"[Compound] direct URL body too short "
            f"({len(body)} chars): {url}"
        )
        return None

    if _looks_like_archive_page(body):
        log(f"[Compound] direct URL returned archive-like page; rejecting: {url}")
        return None

    # Final sanity: the body should mention something close to the title
    # that drove the slug. Skip this check if title is very short.
    return body, r.url  # use final URL after redirects


def _fetch_compound_website_episode(rss_title, session, website_search):
    """Try to locate and scrape the Compound's own episode page.

    Strategy:
      1. Construct the likely permalink from the title and try it directly.
      2. If that 404s or looks wrong, fall back to WordPress site search
         with anchor-text scoring.

    Returns (article_text, url) on success, or None on failure.
    """
    # ---- Path 1: direct slug URL ----------------------------------------
    slug = _wp_slugify(rss_title)
    if slug:
        log(f"[Compound] trying direct permalink for slug: {slug}")
        direct = _try_compound_direct_url(slug, session)
        if direct:
            log(f"[Compound] direct permalink succeeded: {direct[1]}")
            return direct

    # ---- Path 2: WordPress search ---------------------------------------
    if not website_search:
        return None
    tokens = _normalize_title(rss_title).split()
    if not tokens:
        return None
    query = " ".join(tokens[:8])
    search_url = website_search.format(query=quote(query))
    try:
        r = session.get(search_url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as exc:
        log(f"[Compound] website search failed: {exc}")
        return None

    soup = BeautifulSoup(r.content, "html.parser")
    best_url = None
    best_score = 0.0
    best_anchor_text = ""

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            continue
        if "thecompoundnews.com" not in href:
            continue

        # Reject tag pages, category pages, paginated search, search itself.
        parsed = urlparse(href)
        path = parsed.path or "/"
        if any(
            seg in href
            for seg in ("/tag/", "/category/", "/?s=", "/page/", "/author/")
        ):
            continue
        # Reject bare archive/category landing pages.
        if path.lower() in _COMPOUND_ARCHIVE_PATHS:
            continue
        # Reject links with no path depth beyond category (e.g. just a slash).
        if path.count("/") < 2:
            continue

        anchor_text = normalize_whitespace(a.get_text(" ", strip=True))
        if not anchor_text:
            continue

        score = _title_similarity(rss_title, anchor_text)
        if score > best_score:
            best_score = score
            best_url = href
            best_anchor_text = anchor_text

    if best_score < _COMPOUND_ANCHOR_MATCH_THRESHOLD or not best_url:
        log(
            f"[Compound] no strong anchor match on website "
            f"(best score={best_score:.2f}, text='{best_anchor_text[:60]}')"
        )
        return None

    log(
        f"[Compound] trying search-result page (score={best_score:.2f}): "
        f"{best_url}"
    )
    body = fetch_article_text(best_url)
    if not body or len(body) < 400:
        log(f"[Compound] search-result page body too short ({len(body) if body else 0})")
        return None

    if _looks_like_archive_page(body):
        log("[Compound] search-result page looks like an archive/list; rejecting")
        return None

    return body, best_url


def fetch_compound_episode(show_name, feeds, session):
    """Dedicated pipeline for The Compound & Friends.

    Strategy:
      1. Pull latest episode metadata from the podcast RSS (title, pub date).
      2. Fetch the YouTube channel feed; match the latest RSS episode by title.
      3. Try to fetch the YouTube transcript for that matched video.
      4. If transcript fails, scrape the show's website episode page.
      5. Fall back to RSS show notes only as a last resort.
    Always reports in the log which path succeeded.
    """
    rss_url = feeds.get("rss")
    yt_url = feeds.get("youtube")
    site_search = feeds.get("website_search")

    # 1. Load RSS for title + publish date + canonical URL.
    rss_title = "Unknown episode"
    rss_link = "Unavailable"
    rss_published = "Unknown"
    rss_item = None
    if rss_url:
        try:
            r = session.get(rss_url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            rss_soup = BeautifulSoup(r.content, "xml")
            rss_item = rss_soup.find("item")
            if rss_item is not None:
                rss_title = extract_tag_text(rss_item, "title") or rss_title
                rss_link = extract_item_link(rss_item) or rss_link
                rss_published = extract_tag_text(
                    rss_item, "pubDate", "published", "updated", "dc:date"
                ) or rss_published
        except Exception as exc:
            log(f"[Compound] RSS load failed: {exc}")

    # 2. Load YouTube feed and match by title.
    yt_link = None
    yt_title = None
    match_score = 0.0
    if yt_url:
        try:
            r = session.get(yt_url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            yt_soup = BeautifulSoup(r.content, "xml")
            entries = yt_soup.find_all("entry")
            match, match_score = _find_matching_youtube_entry(
                entries, rss_title
            )
            if match is not None:
                yt_title = extract_tag_text(match, "title")
                yt_link = extract_item_link(match)
                log(
                    f"[Compound] matched YouTube video (score={match_score:.2f}): "
                    f"{(yt_title or '')[:70]}"
                )
            else:
                log(
                    f"[Compound] no YouTube title match for '{rss_title[:60]}' "
                    f"(best score={match_score:.2f})"
                )
        except Exception as exc:
            log(f"[Compound] YouTube feed load failed: {exc}")

    # 3. Try YouTube transcript on the matched video.
    if yt_link:
        transcript, tr_err = fetch_youtube_transcript(yt_link)
        if transcript:
            log("[Compound] using YouTube transcript")
            return {
                "title": rss_title,
                "published": rss_published,
                "url": yt_link,
                "data_type": "YouTube transcript",
                "content": transcript,
            }
        else:
            log(f"[Compound] YouTube transcript unavailable ({tr_err})")

    # 4. Fall back to Compound's website episode page.
    website_result = _fetch_compound_website_episode(
        rss_title, session, site_search
    )
    if website_result:
        website_body, website_url = website_result
        log("[Compound] using Compound website episode page")
        return {
            "title": rss_title,
            "published": rss_published,
            "url": website_url,
            "data_type": "Compound website episode page",
            "content": clean_social_noise(website_body),
        }

    # 5. Last resort: RSS show notes.
    if rss_item is not None:
        notes = extract_episode_notes(rss_item)
        log("[Compound] falling back to RSS show notes")
        return {
            "title": rss_title,
            "published": rss_published,
            "url": yt_link or rss_link,
            "data_type": "Show notes (fallback)",
            "content": notes,
        }

    raise RuntimeError(
        "All Compound sources failed: no RSS item, no YouTube match, "
        "no website page"
    )


# ── DISPATCH ──────────────────────────────────────────────────────────────────


def build_podcast_section(session):
    lines = ["### SOCIAL & PODCAST INTELLIGENCE ###"]

    for show_name, feeds in PODCAST_FEEDS.items():
        episode = None
        errors = []

        # Custom handlers get routed first.
        if feeds.get("handler") == "compound":
            try:
                episode = fetch_compound_episode(show_name, feeds, session)
            except Exception as exc:
                errors.append(f"Compound handler: {exc}")
        else:
            if "youtube" in feeds:
                try:
                    episode = fetch_youtube_episode(
                        show_name, feeds["youtube"], session
                    )
                except Exception as exc:
                    errors.append(f"YouTube: {exc}")

            if episode is None and "rss" in feeds:
                try:
                    episode = fetch_rss_episode(
                        show_name, feeds["rss"], session
                    )
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
