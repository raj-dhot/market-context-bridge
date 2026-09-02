import datetime as dt
import html
import logging
import re
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from curl_cffi import requests
import trafilatura
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Origin-publisher RSS discovery. SOFT import on purpose.
#
# If news_sources.py has not landed yet (or was renamed), a hard import would
# raise at module load and kill the month outright. Degrading to the old
# aggregator-only path instead still produces a file, the workflow's feed
# preflight step already warned, and the CITABLE gate in the health check will
# catch the degraded yield. Loud, not fatal.
try:
    import news_sources as nsrc
    PUBLISHER_FEEDS_AVAILABLE = True
except ImportError as _exc:
    nsrc = None
    PUBLISHER_FEEDS_AVAILABLE = False
    logging.error(
        "news_sources.py could not be imported (%s). Falling back to "
        "aggregator-only discovery, which yields uncitable MSN and Google "
        "News stubs. Put news_sources.py next to fetch_news.py.", _exc
    )

# curl_cffi impersonation target. Kept as a hedge: on GitHub Actions
# (datacenter IPs) some publishers reject non-browser TLS fingerprints, and
# impersonation can recover those extractions. It does NOT help with msn.com
# (whose body is JavaScript-rendered and simply absent from the HTML), which is
# why those are skipped entirely below.
IMPERSONATE = "chrome124"

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

# FALLBACK ONLY as of 2026-09-02. Origin-publisher feeds in news_sources.py are
# the primary discovery path; these queries now only top up a category whose
# publisher feeds came back with too few EXTRACTABLE items.
#
# Why the demotion: Bing News is a Microsoft product that heavily indexes MSN,
# also Microsoft, whose article body is JavaScript-rendered and absent from the
# HTML. Google News RSS returns Google's own opaque redirect stubs. Both hosts
# are therefore in UNEXTRACTABLE_HOSTS and fall back to a bare headline. Asking
# two aggregators for links got back links to themselves: 6 of 9 items arrived
# uncitable on both 2026-09-01 and 2026-09-02, with two of three categories
# entirely unusable.
#
# The keys MUST stay identical to news_sources.PUBLISHER_FEEDS keys. They are
# matched by string. A renamed category here silently disables its publisher
# feeds and drops that category back to aggregator search with no error.
#
# NOTE: Do NOT use the boolean operator "OR" in these queries. Bing News RSS
# treats a raw "OR" literally and returns ZERO results. Use plain space-
# separated keywords instead.
NEWS_QUERIES = {
    "Canada (TSX & Macro)": "TSX Bank of Canada inflation GDP",
    "United States (S&P 500 & Fed)": "S&P 500 Federal Reserve inflation jobs",
    "International & Emerging": "emerging markets international equities",
    "Competitor & AI Pulse": "Wealthsimple Questrade AI wealth management Canada",
}

# Do NOT set a User-Agent here. curl_cffi's impersonate=IMPERSONATE already
# installs a complete, self-consistent browser header set (User-Agent + the
# matching sec-ch-ua hints) that lines up with its TLS/JA3 fingerprint.
# Overriding the User-Agent by hand desynchronizes those and can actually
# TRIGGER the anti-bot challenges we are trying to avoid. Only add headers
# that impersonation does not already cover.
HEADERS = {
    "Accept-Language": "en-CA,en;q=0.9",
}

BING_NEWS_RSS_BASE = "https://www.bing.com/news/search?q={query}&format=rss&count=15"
# Second, independent discovery source. Used to top up a category when Bing
# returns too few recent candidates (or is blocked/empty).
GOOGLE_NEWS_RSS_BASE = (
    "https://news.google.com/rss/search?q={query}&hl=en-CA&gl=CA&ceid=CA:en"
)

# Hosts we never try to full-text extract; we go straight to the RSS
# headline/snippet instead. Two reasons a host lands here:
#   * msn.com  - body is JavaScript-rendered and absent from static HTML
#                (verified: no articleBody/JSON-LD/embedded data; content API
#                returns HTTP 400). Unrecoverable by scraping.
#   * news.google.com - Google News links are opaque redirect stubs, not the
#                article; extraction always yields ~nothing, so fetching them
#                just wastes time. Use their title/snippet directly instead.
UNEXTRACTABLE_HOSTS = ("msn.com", "news.google.com")

REQUEST_TIMEOUT = 20           # RSS feed fetches
ARTICLE_TIMEOUT = 12           # individual article-body fetches (bound CI time)
NEWS_ITEM_TARGET = 3           # articles to surface per category
NEWS_CANDIDATE_CAP = 25        # max RSS items to consider per category
NEWS_MAX_FETCH_ATTEMPTS = 8    # max full-text extraction attempts per category
NEWS_LOOKBACK_DAYS = 31        # trailing month, to match the monthly brief
NEWS_BODY_MIN_LENGTH = 300     # min chars to count an extraction as full text
NEWS_SNIPPET_MIN_LENGTH = 80   # min chars for an RSS snippet to be usable
NEWS_BODY_MAX_CHARS = 2000
EPISODE_TEXT_MAX_CHARS = 4000
OUTPUT_FILE = Path("latest_news.txt")
YOUTUBE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_SHORTS_PATTERN = re.compile(r"youtube\.com/shorts/", re.IGNORECASE)

# Delay between news RSS fetches (be polite)
NEWS_FETCH_DELAY = 2


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
    cleaned = re.sub(r"[⁠‌‍﻿­]+", "", cleaned)

    # Common emoji ranges
    cleaned = re.sub(
        r"[\U0001F300-\U0001FAD6\U0001F600-\U0001F64F"
        r"\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF"
        r"☀-⛿✀-➿]+",
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


def fetch_article_text(url, session=None, favor_recall=False):
    """Download an article and return its main text (str) or None.

    Skips hosts whose body is JavaScript-rendered (see UNEXTRACTABLE_HOSTS).

    favor_recall defaults to False so the pre-existing PODCAST caller
    (fetch_rss_episode) keeps its original extraction behavior. The news
    pipeline opts in with favor_recall=True so borderline article pages still
    yield text.
    """
    host = urlparse(url).netloc.lower()
    if any(bad in host for bad in UNEXTRACTABLE_HOSTS):
        logging.info(f"Skipping unextractable host ({host}): {url[:80]}")
        return None

    try:
        if session is not None:
            response = session.get(url, timeout=ARTICLE_TIMEOUT)
        else:
            response = requests.get(
                url, impersonate=IMPERSONATE, timeout=ARTICLE_TIMEOUT
            )

        if response.status_code != 200:
            logging.warning(f"Failed to fetch {url[:80]}: HTTP {response.status_code}")
            return None
        extracted = trafilatura.extract(
            response.text,
            include_comments=False,
            include_links=False,
            favor_recall=favor_recall,
        )
        cleaned = normalize_whitespace(extracted)
        return cleaned or None
    except Exception as exc:
        logging.error(f"Error extracting text from {url[:80]}: {exc}")
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


# ── NEWS DISCOVERY (BING + GOOGLE NEWS RSS) ────────────────────────────────────

def _decode_bing_url(link):
    """Bing wraps each result in an apiclick.aspx redirect carrying the real
    publisher URL in the `url=` query param. Decode it so we fetch the
    publisher directly and can see the real host (e.g. detect msn.com)."""
    if not link:
        return link
    if "bing.com/news/apiclick" in link:
        real = parse_qs(urlparse(link).query).get("url", [None])[0]
        if real:
            return real
    return link


def _news_dedupe_key(title):
    # Full normalized title (punctuation/case-insensitive). Do NOT truncate to
    # a prefix - distinct stories that share a long common headline prefix
    # (e.g. daily market recaps) would otherwise collapse into one.
    return re.sub(r"[^\w\s]", "", (title or "").lower()).strip()


def _is_extractable(item):
    """True when the item is recent AND its body can actually be fetched.

    Nothing in the old pipeline measured this, which is why two categories
    could be entirely unusable while every check reported healthy. The old
    top-up trigger counted items with an in-window DATE, so three MSN stubs
    counted as three good candidates. It did still fire the top-up at that
    count, but it topped up from a SECOND aggregator that returned more
    stubs, so more fetching could not help: the candidate pool was 6 stubs
    and 0 citable items.

    A stub is a PRESENT item, not a missing one, so no degradation marker
    fires for it. Counting extractability is what makes the difference
    visible, and it is used twice below: to decide whether the aggregators
    are needed at all, and to order candidates so a citable item is never
    crowded out of a slot by a stub that merely sorted earlier.
    """
    if not item.get("published"):
        return False
    if parse_datetime(item["published"]) is None:
        return False
    if not is_recent(item["published"]):
        return False
    host = urlparse(item.get("url") or "").netloc.lower()
    return not any(bad in host for bad in UNEXTRACTABLE_HOSTS)


def fetch_bing_news_rss(query, session, max_items=15):
    """Fetch news candidates from Bing News RSS.

    Returns a list of dicts: title, url (decoded publisher URL), source,
    published, desc (RSS snippet), origin.
    """
    feed_url = BING_NEWS_RSS_BASE.format(query=quote(query))
    logging.info(f"Fetching Bing News RSS for: '{query}'")
    try:
        resp = session.get(feed_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        logging.error(f"Bing News RSS fetch failed for '{query}': {exc}")
        return []

    soup = BeautifulSoup(resp.content, "xml")
    items = soup.find_all("item")
    logging.info(f"Found {len(items)} Bing News items for '{query}'")
    results = []

    for item in items[:max_items]:
        title = extract_tag_text(item, "title") or "Untitled"
        link = _decode_bing_url(extract_tag_text(item, "link"))
        pub_date = extract_tag_text(item, "pubDate")
        desc = clean_social_noise(html_to_text(extract_tag_text(item, "description")))
        source_tag = find_first_tag(item, "news:source", "source")
        source = (
            normalize_whitespace(source_tag.get_text(" ", strip=True))
            if source_tag
            else ""
        )
        if not link:
            continue
        results.append({
            "title": title,
            "url": link,
            "source": source or source_name_from_url(link),
            "published": pub_date,
            "desc": desc,
            "origin": "Bing",
        })

    return results


def fetch_google_news_rss(query, session, max_items=10):
    """Second discovery source, used to top up when Bing is thin/blocked.

    Google News wraps links in an encoded redirect; we keep that URL as-is
    (it resolves in a browser) and rely on the title/source/snippet.
    """
    feed_url = GOOGLE_NEWS_RSS_BASE.format(query=quote(query))
    logging.info(f"Fetching Google News RSS for: '{query}'")
    try:
        resp = session.get(feed_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        logging.error(f"Google News RSS fetch failed for '{query}': {exc}")
        return []

    soup = BeautifulSoup(resp.content, "xml")
    items = soup.find_all("item")
    logging.info(f"Found {len(items)} Google News items for '{query}'")
    results = []

    for item in items[:max_items]:
        title = extract_tag_text(item, "title") or "Untitled"
        link = extract_tag_text(item, "link")
        pub_date = extract_tag_text(item, "pubDate")
        desc = clean_social_noise(html_to_text(extract_tag_text(item, "description")))
        source_tag = find_first_tag(item, "source")
        source = (
            normalize_whitespace(source_tag.get_text(" ", strip=True))
            if source_tag
            else "Google News"
        )
        if not link:
            continue
        results.append({
            "title": title,
            "url": link,
            "source": source,
            "published": pub_date,
            "desc": desc,
            "origin": "GoogleNews",
        })

    return results


# ── NEWS SECTION ──────────────────────────────────────────────────────────────

def build_news_section():
    """Build the news section.

    Core strategy:
      1. PRIMARY: gather candidates from origin-publisher RSS feeds
         (news_sources.py). Real article bodies, canonical URLs, and
         publishers that pass the SKILL.md section 4 source hierarchy by
         construction.
      2. FALLBACK: top up from Bing / Google News RSS only when the publisher
         feeds did not yield enough EXTRACTABLE recent candidates.
      3. PREFER full-text: extract each candidate's body, skipping JS-only
         hosts (msn.com) that can never yield text.
      4. FALL BACK to the RSS headline+snippet for any slot not filled with
         full text (richest snippets first). A category is therefore never
         empty just because extraction failed.
    """
    lines = []
    query_list = list(NEWS_QUERIES.items())

    # Persistent TLS impersonation + connection pooling across the section.
    with requests.Session(impersonate=IMPERSONATE) as session:
        session.headers.update(HEADERS)

        for idx, (category, query) in enumerate(query_list):
            lines.append(f"### {category.upper()} ###")

            # ── PRIMARY: origin-publisher feeds ─────────────────────────────
            # A dead feed is logged and skipped inside gather_category rather
            # than raised, so one publisher cannot take out the category.
            candidates = []
            if PUBLISHER_FEEDS_AVAILABLE:
                try:
                    candidates = nsrc.gather_category(
                        category, session,
                        html_to_text=html_to_text,
                        clean_noise=clean_social_noise,
                    )
                except Exception as exc:
                    logging.error(
                        f"Publisher feeds failed for '{category}': {exc}")
                    candidates = []

            # ── FALLBACK: aggregator top-up ─────────────────────────────────
            # Counts EXTRACTABLE items, not merely recent ones, so the
            # aggregators are consulted only when the publisher feeds actually
            # came up short. Under the old count three MSN stubs read as three
            # good candidates, and the top-up that fired pulled from a second
            # aggregator that returned more stubs.
            usable = {
                _news_dedupe_key(c["title"])
                for c in candidates if _is_extractable(c)
            }

            # Some categories would rather be EMPTY than full of stubs. See
            # news_sources.NO_AGGREGATOR_FALLBACK for the reasoning: the AI
            # objection script has a documented no-hook path, so a stub in
            # Competitor & AI Pulse costs a slot and some research time and
            # returns nothing citable.
            no_fallback = (
                PUBLISHER_FEEDS_AVAILABLE
                and category in getattr(nsrc, "NO_AGGREGATOR_FALLBACK", ())
            )
            if no_fallback and len(usable) < NEWS_ITEM_TARGET + 1:
                logging.info(
                    f"[{category}] {len(usable)} extractable item(s) and "
                    f"aggregator fallback is disabled for this category. "
                    f"Shipping what the publisher feeds returned; an empty or "
                    f"thin section here is an accepted outcome."
                )

            if not no_fallback and len(usable) < NEWS_ITEM_TARGET + 1:
                logging.info(
                    f"[{category}] only {len(usable)} extractable publisher "
                    f"item(s); topping up from aggregator search"
                )
                try:
                    candidates += fetch_bing_news_rss(query, session)
                    candidates += fetch_google_news_rss(query, session)
                except Exception as exc:
                    logging.error(f"News fetch error for '{category}': {exc}")
                    # Only a hard error when BOTH paths produced nothing. The
                    # marker text is preserved verbatim because the workflow
                    # health check and SKILL.md section 1 both grep for it.
                    if not candidates:
                        lines.append(f"News fetch error: {exc}")
                        lines.append("")
                        if idx < len(query_list) - 1:
                            time.sleep(NEWS_FETCH_DELAY)
                        continue

            # Extractable candidates first. The loop below stops at
            # NEWS_ITEM_TARGET, so ordering decides what gets extracted, and a
            # citable item must never be crowded out by a stub that merely
            # appeared earlier in the candidate list. sorted() is stable, so
            # ordering within each group is preserved.
            candidates.sort(key=lambda c: not _is_extractable(c))

            chosen = []
            snippet_pool = []
            seen = set()
            attempts = 0
            stale = 0

            for item in candidates[:NEWS_CANDIDATE_CAP]:
                if len(chosen) >= NEWS_ITEM_TARGET:
                    break

                if item["published"] and not is_recent(item["published"]):
                    stale += 1
                    continue

                key = _news_dedupe_key(item["title"])
                if not key or key in seen:
                    continue
                seen.add(key)

                host = urlparse(item["url"]).netloc.lower()
                skip_fetch = (
                    any(bad in host for bad in UNEXTRACTABLE_HOSTS)
                    or attempts >= NEWS_MAX_FETCH_ATTEMPTS
                )

                body = None
                if not skip_fetch:
                    attempts += 1
                    logging.info(f"Fetching Article: {item['url'][:80]}...")
                    body = fetch_article_text(item["url"], session, favor_recall=True)
                    time.sleep(0.5)  # gentle throttle between publisher hits

                if body and len(body) >= NEWS_BODY_MIN_LENGTH:
                    item["content"] = truncate_text(body, NEWS_BODY_MAX_CHARS)
                    item["content_type"] = "full-text"
                    chosen.append(item)
                    logging.info(
                        f"OK full-text ({len(body)} chars) [{item['origin']}] "
                        f"{host}: {item['title'][:60]}"
                    )
                else:
                    # No usable full text -> keep as a snippet-backfill candidate.
                    snippet_pool.append(item)

            # Backfill remaining slots with the best available snippet, richest
            # first. Prefer the RSS description; fall back to the headline when
            # the description is too thin (common for Google News items) so a
            # slot can still be filled rather than left empty.
            snippet_pool.sort(key=lambda it: -len(it["desc"]))
            for item in snippet_pool:
                if len(chosen) >= NEWS_ITEM_TARGET:
                    break
                snippet = (
                    item["desc"]
                    if len(item["desc"]) >= NEWS_SNIPPET_MIN_LENGTH
                    else item["title"]
                )
                if not snippet:
                    continue
                item["content"] = truncate_text(snippet, NEWS_BODY_MAX_CHARS)
                item["content_type"] = "snippet"
                chosen.append(item)
                logging.info(
                    f"OK snippet ({len(snippet)} chars) [{item['origin']}]: "
                    f"{item['title'][:60]}"
                )

            n_full = sum(1 for c in chosen if c["content_type"] == "full-text")
            logging.info(
                f"[{category}] chosen={len(chosen)} (target {NEWS_ITEM_TARGET}), "
                f"CITABLE={n_full}, stubs={len(chosen) - n_full}, "
                f"stale_skipped={stale}, fetch_attempts={attempts}"
            )
            if n_full == 0 and chosen:
                logging.warning(
                    f"[{category}] every item is a headline stub with no "
                    f"article body. The brief cannot cite any of them."
                )

            if not chosen:
                lines.append("No recent articles found for this query.")
                lines.append("")
            else:
                for item in chosen:
                    tag = "" if item["content_type"] == "full-text" else " (headline summary)"
                    lines.extend([
                        f"TITLE: {item['title']}",
                        f"SOURCE: {item['source']}{tag}",
                        f"PUBLISHED: {item['published'] or 'Unknown'}",
                        f"URL: {item['url']}",
                        f"CONTENT: {item['content']}",
                        "",
                    ])

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

    transcript = fetch_youtube_transcript(link)
    if transcript:
        data_type = "Transcript"
        content = transcript
    else:
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
        page_content = fetch_article_text(link, session)
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
    with requests.Session(impersonate=IMPERSONATE) as session:
        session.headers.update(HEADERS)
        sections.append(build_podcast_section(session))
    return "\n".join(section for section in sections if section).strip() + "\n"


def fetch_content(output_file=OUTPUT_FILE):
    logging.info("Starting Oceanfront Market Intelligence Generation...")
    if not PUBLISHER_FEEDS_AVAILABLE:
        logging.warning(
            "Running WITHOUT publisher feeds. Expect mostly uncitable stubs."
        )
    report = build_report()
    output_file.write_text(report, encoding="utf-8")
    logging.info(f"Intelligence report successfully written to {output_file}")


if __name__ == "__main__":
    fetch_content()
