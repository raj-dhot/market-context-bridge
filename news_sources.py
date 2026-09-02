"""
news_sources.py  --  origin-publisher RSS discovery for the Advisor Pulse.

Drop this next to fetch_news.py in the GitHub repo. See FETCH_NEWS_PATCH.md
for the three edits that wire it in.

    python3 news_sources.py --check-feeds      # validate every feed, exit 1 on failure


WHY THIS EXISTS
===============
The fetcher discovered articles from two search aggregators:

    BING_NEWS_RSS_BASE   = bing.com/news/search?q={query}&format=rss
    GOOGLE_NEWS_RSS_BASE = news.google.com/rss/search?q={query}

Each is structurally biased toward its own unextractable property. Bing News
is a Microsoft product that heavily indexes MSN, also Microsoft, whose article
body is JavaScript-rendered and absent from the HTML. Google News RSS returns
Google's own opaque CBMi... redirect stubs, which are not the article. Both
hosts are therefore in UNEXTRACTABLE_HOSTS and fall back to a bare headline.

So the 6-of-9 unusable rate was not bad luck. It was the architecture: the
fetcher asked two aggregators for links and each handed back links to itself.
On both 2026-09-01 and 2026-09-02 the same shape appeared, 3 MSN stubs plus 3
Google stubs, with NORTH AMERICA and COMPETITOR & AI PULSE entirely unusable.

Escalating the scraper does not fix this. A headless browser would render MSN,
but MSN is an aggregator syndicating The Canadian Press, and SKILL.md section 4
forbids sourcing a headline figure from an aggregator. You would be building a
browser to render pages the brief is not allowed to quote.

THE FIX: ask the publishers directly. Every feed below is an origin publisher
that SKILL.md's own source hierarchy already prefers, serving static XML with
canonical URLs. Real bodies for trafilatura, no JS, no redirect stubs, no
bot hostility, no API key, no cost, and NO NEW DEPENDENCIES: this module needs
only BeautifulSoup and the session that fetch_news.py already builds.

THE TRADE-OFF, STATED HONESTLY. Publisher feeds are topic-broad rather than
query-targeted, so keyword targeting moves client-side into
matches_category(). That is a real loss of precision and the reason the
aggregators are KEPT as a top-up path rather than deleted: if a category comes
up short on citable items, fetch_news.py still falls back to them. The goal is
to raise the floor of usable input, not to eliminate search.
"""

from __future__ import annotations

import logging
import sys
import time
from urllib.parse import urlparse

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests
except ImportError:                                          # pragma: no cover
    requests = None


# ══════════════════════════════════════════════════════════════════════════════
# FEED REGISTRY
# ══════════════════════════════════════════════════════════════════════════════
#
# VERIFIED means the URL was confirmed on 2026-09-02, most authoritatively for
# the Bank of Canada, whose paths were read off the Bank's own RSS index at
# bankofcanada.ca/rss-feeds. UNVERIFIED feeds are commented out: they are
# plausible and desirable but were NOT confirmed, and a wrong URL is a silently
# dead category. Run --check-feeds, then uncomment whatever passes.
#
# Ordering matters. Items are gathered in list order and the primary
# institution comes first, which matches the source hierarchy in SKILL.md
# section 4: primary institution, then major outlet with a named desk, then
# reputable specialist.

PUBLISHER_FEEDS = {
    "North America (TSX & S&P 500)": [
        # --- primary institutions (top of the source hierarchy) -------------
        ("Bank of Canada",
         "https://www.bankofcanada.ca/content_type/press-releases/feed/"),
        ("Bank of Canada",
         "https://www.bankofcanada.ca/utility/news/feed/"),
        ("Statistics Canada",
         "https://www150.statcan.gc.ca/n1/rss/dai-quo/0-eng.atom"),
        # --- major outlets, named desks -------------------------------------
        ("CBC Business",
         "https://www.cbc.ca/webfeed/rss/rss-business"),
        # UNVERIFIED. Postmedia moved business.financialpost.com to
        # financialpost.com; which host still serves the feed was not
        # confirmed. Check both, keep the one that passes.
        # ("Financial Post", "https://financialpost.com/feed/"),
        # ("Financial Post", "https://business.financialpost.com/feed/"),
        # UNVERIFIED.
        # ("Globe and Mail", "https://www.theglobeandmail.com/business/rss/"),
    ],

    "International & Emerging": [
        ("CBC Business",
         "https://www.cbc.ca/webfeed/rss/rss-business"),
        ("Bank of Canada",
         "https://www.bankofcanada.ca/content_type/publications/feed/"),
        # UNVERIFIED. Reuters withdrew most public RSS around 2020-2023, so
        # treat any Reuters feed as unlikely until --check-feeds says otherwise.
        # ("Reuters Business", "https://www.reuters.com/business/rss"),
    ],

    # This category is the one the aggregators served worst and it is the
    # anchor for the AI objection script every month. BetaKit carried the best
    # competitor development in BOTH recent editions (Questrade's agentic
    # finance launch), and both times it arrived as an unreadable Google stub
    # that had to be recovered by hand. Subscribing directly to the source
    # that keeps winning is the single highest-value change in this file.
    "Competitor & AI Pulse": [
        ("BetaKit", "https://betakit.com/feed/"),
        # UNVERIFIED but high value: the Canadian advice-industry trades.
        # ("Wealth Professional", "https://www.wealthprofessional.ca/rss"),
        # ("Investment Executive", "https://www.investmentexecutive.com/feed/"),
    ],
}


# Publisher feeds carry everything the desk published, so relevance is filtered
# here instead of by a search engine. A candidate is kept when its title or
# snippet contains any term. Keep terms broad: a false positive is a wasted
# extraction attempt, while a false negative silently drops the month's story.
CATEGORY_KEYWORDS = {
    "North America (TSX & S&P 500)": [
        "tsx", "s&p", "stock", "equit", "market", "index", "wall street",
        "interest rate", "policy rate", "inflation", "cpi", "gdp",
        "recession", "bond", "yield", "earnings", "dollar", "loonie",
        "tariff", "trade", "federal reserve", "monetary policy",
        # NOT "bank of canada": on a Bank of Canada feed the institution name
        # matches every item, including the museum's opening hours, so it
        # discriminates nothing. NOT bare "fed" either: it substring-matches
        # "feed", "federated" and "federal" and would pass anything.
    ],
    "International & Emerging": [
        "emerging", "international", "global", "china", "india", "japan",
        "europe", "euro", "asia", "latin america", "brazil", "mexico",
        "oil", "commodit", "currency", "geopolit", "imf", "world bank",
        "msci", "export", "supply chain",
    ],
    "Competitor & AI Pulse": [
        "wealthsimple", "questrade", "robo", "advisor", "adviser",
        "wealth management", "brokerage", "fintech", "fin tech",
        "artificial intelligence", "agentic", "automat", "robinhood",
        "invest", "portfolio", "ci direct", "investease", "smartfolio",
        "planner", "fee", "custodian",
        # Bare "ai" is deliberately NOT a term: it substring-matches "said",
        # "rail", "detail", "campaign" and would pass almost every headline.
    ],
}

# Institutional feeds carry corporate housekeeping alongside the economics:
# museum hours, counterfeit awards, job postings, bank-note design. Those
# items often match an include term (or the institution's own name) while
# being useless to the brief, so they are excluded outright. Applied AFTER
# the include filter and to the title only, so a passing mention in an
# article body cannot drop a real story.
EXCLUDE_TERMS = (
    "museum", "counterfeit", "career", "scholarship", "job posting",
    "bank note", "banknote", "unclaimed", "labour negotiation",
    "award", "obituary", "appointment to the board",
)

FEED_TIMEOUT = 20          # per-feed fetch
FEED_DELAY = 1.0           # politeness between feeds
FEED_MAX_ITEMS = 40        # per feed, before filtering


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _text_of(parent, *names):
    """First non-empty text among the named child tags. RSS and Atom differ in
    tag names for the same concept, so callers pass every spelling."""
    for name in names:
        tag = parent.find(name)
        if tag:
            txt = tag.get_text(" ", strip=True)
            if txt:
                return " ".join(txt.split())
    return ""


def _link_of(item):
    """Best URL from an RSS <item> or an Atom <entry>.

    Atom puts the URL in link/@href and may carry several rel= variants;
    rel="alternate" (or absent) is the article. RSS puts it in link's text.
    """
    for tag in item.find_all("link"):
        rel = (tag.get("rel") or ["alternate"])
        rel = rel[0] if isinstance(rel, list) else rel
        href = tag.get("href")
        if href and rel in ("alternate", "self", None, ""):
            return href.strip()
        txt = tag.get_text(" ", strip=True)
        if txt.startswith("http"):
            return txt.strip()
    guid = _text_of(item, "guid", "id")
    return guid.strip() if guid.startswith("http") else None


def matches_category(item, terms):
    """True when the item looks relevant to the category.

    Include terms match title OR snippet, so a story whose headline is coy
    still qualifies on its summary. Exclude terms match the TITLE ONLY: a
    real market story that happens to mention the word "award" in its body
    must not be dropped.
    """
    title = str(item.get("title", "")).lower()
    if any(bad in title for bad in EXCLUDE_TERMS):
        return False
    if not terms:
        return True
    hay = "%s %s" % (title, str(item.get("desc", "")).lower())
    return any(t in hay for t in terms)


# ══════════════════════════════════════════════════════════════════════════════
# FETCH
# ══════════════════════════════════════════════════════════════════════════════

def fetch_publisher_rss(publisher, feed_url, session, *,
                        html_to_text, clean_noise,
                        max_items=FEED_MAX_ITEMS, timeout=FEED_TIMEOUT):
    """Fetch one origin-publisher feed.

    Returns items in EXACTLY the dict shape fetch_bing_news_rss returns, so
    build_news_section needs no change to its downstream handling:
        {title, url, source, published, desc, origin}

    html_to_text and clean_noise are injected rather than imported to keep
    this module free of any dependency on fetch_news.py, which would otherwise
    be a circular import.
    """
    logging.info("Fetching publisher feed: %s", publisher)
    try:
        resp = session.get(feed_url, timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:                                 # noqa: BLE001
        logging.error("Publisher feed failed (%s): %s", publisher, exc)
        return []

    soup = BeautifulSoup(resp.content, "xml")
    entries = soup.find_all("item") or soup.find_all("entry")
    logging.info("  %s: %d raw item(s)", publisher, len(entries))

    out = []
    for entry in entries[:max_items]:
        title = _text_of(entry, "title") or "Untitled"
        url = _link_of(entry)
        if not url:
            continue
        # RSS: pubDate. Atom: published, else updated.
        published = _text_of(entry, "pubDate", "published", "updated",
                             "dc:date")
        raw_desc = _text_of(entry, "description", "summary", "content",
                            "content:encoded")
        desc = clean_noise(html_to_text(raw_desc)) if raw_desc else ""
        out.append({
            "title": title,
            "url": url,
            "source": publisher,          # real publisher, not an aggregator
            "published": published,
            "desc": desc,
            "origin": "Publisher",
        })
    return out


def gather_category(category, session, *, html_to_text, clean_noise,
                    keyword_filter=True):
    """All candidates for one category, from every configured publisher feed.

    Feed failures are logged and skipped rather than raised: one dead
    publisher must not take out the category, which is the same
    fail-soft posture the rest of the fetcher uses.
    """
    feeds = PUBLISHER_FEEDS.get(category, [])
    terms = CATEGORY_KEYWORDS.get(category, []) if keyword_filter else []

    candidates, seen_urls = [], set()
    for i, (publisher, feed_url) in enumerate(feeds):
        items = fetch_publisher_rss(
            publisher, feed_url, session,
            html_to_text=html_to_text, clean_noise=clean_noise)

        kept = 0
        for it in items:
            if it["url"] in seen_urls:
                continue          # same story in two feeds, e.g. CBC in both
            if not matches_category(it, terms):
                continue
            seen_urls.add(it["url"])
            candidates.append(it)
            kept += 1
        logging.info("  %s: %d kept after keyword filter", publisher, kept)

        if i < len(feeds) - 1:
            time.sleep(FEED_DELAY)

    logging.info("[%s] %d publisher candidate(s) total", category,
                 len(candidates))
    return candidates


# ══════════════════════════════════════════════════════════════════════════════
# PREFLIGHT
# ══════════════════════════════════════════════════════════════════════════════

def check_feeds(verbose=True):
    """Validate every configured feed. Returns (ok_count, failures).

    Run this in CI before the scraper. A feed that 404s or stops being XML is
    otherwise indistinguishable from a quiet news month: the category just
    comes back thin and nobody knows why.
    """
    if requests is None:
        print("curl_cffi is not installed; run inside the fetcher's env.")
        return 0, [("*", "curl_cffi missing")]

    failures, ok = [], 0
    with requests.Session(impersonate="chrome124") as session:
        session.headers.update({"Accept-Language": "en-CA,en;q=0.9"})
        for category, feeds in PUBLISHER_FEEDS.items():
            if verbose:
                print("\n%s" % category)
            for publisher, url in feeds:
                try:
                    r = session.get(url, timeout=FEED_TIMEOUT)
                    n = 0
                    if r.status_code == 200:
                        soup = BeautifulSoup(r.content, "xml")
                        n = len(soup.find_all("item") or soup.find_all("entry"))
                    if r.status_code != 200:
                        failures.append((publisher, "HTTP %d" % r.status_code))
                        if verbose:
                            print("  FAIL  %-22s HTTP %d  %s"
                                  % (publisher, r.status_code, url))
                    elif n == 0:
                        failures.append((publisher, "0 items / not XML"))
                        if verbose:
                            print("  FAIL  %-22s 0 items (not XML?)  %s"
                                  % (publisher, url))
                    else:
                        ok += 1
                        if verbose:
                            print("  ok    %-22s %3d items  %s"
                                  % (publisher, n, urlparse(url).netloc))
                except Exception as exc:                     # noqa: BLE001
                    failures.append((publisher, str(exc)[:80]))
                    if verbose:
                        print("  FAIL  %-22s %s" % (publisher, str(exc)[:60]))
                time.sleep(FEED_DELAY)
    return ok, failures


def main(argv):
    if "--check-feeds" not in argv:
        print(__doc__.strip().split("\n\n")[0])
        print("\nusage: python3 news_sources.py --check-feeds")
        return 2

    logging.basicConfig(level=logging.WARNING)
    print("=" * 70)
    print("ADVISOR PULSE  |  PUBLISHER FEED PREFLIGHT")
    print("=" * 70)
    ok, failures = check_feeds()
    total = sum(len(v) for v in PUBLISHER_FEEDS.values())
    print("\n" + "-" * 70)
    print("%d of %d feed(s) healthy" % (ok, total))
    if failures:
        print("\nFailures:")
        for pub, why in failures:
            print("  %-22s %s" % (pub, why))
        print("\nA dead feed looks exactly like a quiet news month downstream.")
        print("Fix or comment out the URL before relying on the category.")
        return 1
    print("All configured feeds healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
