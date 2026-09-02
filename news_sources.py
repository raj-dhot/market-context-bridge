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
    # ── CANADA ──────────────────────────────────────────────────────────────
    # Split out of the old combined "North America" category on 2026-09-02.
    # One category could not serve both countries: every feed in it was
    # Canadian, so US coverage arrived only as a side-mention inside Canadian
    # reporting and the S&P 500 had no source of its own.
    "Canada (TSX & Macro)": [
        # primary institutions, top of the SKILL.md section 4 hierarchy
        ("Bank of Canada",
         "https://www.bankofcanada.ca/content_type/press-releases/feed/"),
        ("Bank of Canada",
         "https://www.bankofcanada.ca/utility/news/feed/"),
        ("Statistics Canada",
         "https://www150.statcan.gc.ca/n1/rss/dai-quo/0-eng.atom"),
        # major outlets, named desks
        ("CBC Business",
         "https://www.cbc.ca/webfeed/rss/rss-business"),
        ("Financial Post",
         "https://financialpost.com/feed/"),
        # CONFIRMED DEAD 2026-09-02, do not retry:
        #   theglobeandmail.com/business/?service=rss -> serves HTML, and the
        #     Globe is paywalled so extraction would get a teaser at best.
    ],

    # ── UNITED STATES ───────────────────────────────────────────────────────
    # Both institutions here are primary sources and neither is paywalled.
    # The Fed and BLS publish the actual decisions and the actual CPI and
    # payroll prints, which is what the brief should quote rather than a
    # secondhand market recap.
    "United States (S&P 500 & Fed)": [
        # VERIFIED 2026-09-02: RSS 2.0
        ("Federal Reserve",
         "https://www.federalreserve.gov/feeds/press_all.xml"),
        # VERIFIED 2026-09-02: RSS 2.0. CPI, payrolls, unemployment.
        ("Bureau of Labor Statistics",
         "https://www.bls.gov/feed/bls_latest.rss"),
        # VERIFIED 2026-09-02: RSS 2.0. Named in the SKILL.md source
        # hierarchy, and needed for index-level and market-reaction coverage
        # that the institutions do not publish.
        ("CNBC",
         "https://search.cnbc.com/rs/search/combinedcms/view.xml"
         "?partnerId=wrss01&id=20910258"),
        # VERIFIED 2026-09-02 as an alternative if CNBC 403s from CI:
        # ("MarketWatch",
        #  "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
        # CONFIRMED DEAD 2026-09-02: bea.gov/rss.xml -> 404.
    ],

    # ── INTERNATIONAL & EMERGING ────────────────────────────────────────────
    # FEEDS HERE MUST BE NON-NORTH-AMERICAN. This category previously ran on
    # CBC Business and Bank of Canada publications, both Canadian, and on
    # 2026-09-02 returned three Canadian/US stories. All were full-text and
    # citable, so every automated check passed while the section was not
    # international and one item duplicated the Canada theme. No keyword
    # filter fixes that; the feeds were the bug.
    #
    # Feed sets are DISJOINT across categories, which also removes the
    # duplicate risk: gather_category dedupes by URL within a category but
    # not across them, so a shared feed could file one article in two places.
    "International & Emerging": [
        # VERIFIED 2026-09-02: RSS 2.0. Primary institution for the euro area.
        ("European Central Bank",
         "https://www.ecb.europa.eu/rss/press.html"),
        # VERIFIED 2026-09-02: RSS 2.0. Major outlet, named desk, global
        # business coverage, not paywalled, long-stable feed.
        ("BBC Business",
         "https://feeds.bbci.co.uk/news/business/rss.xml"),
        # VERIFIED 2026-09-02: RSS 1.0 / RDF, which the parser handles since
        # RDF still uses <item>. German public broadcaster, Europe and Asia.
        ("DW Business",
         "https://rss.dw.com/rdf/rss-en-bus"),

        # REMOVED 2026-09-02 on source-independence grounds, NOT because the
        # feed failed. aljazeera.com/xml/rss/all.xml returns valid RSS.
        # Al Jazeera is funded by the Qatari state, and Qatar is one of the
        # world's largest LNG exporters. This brief's current lead macro
        # story is Middle East energy prices driving inflation, which makes
        # Qatar an INTERESTED PARTY in exactly the story we would be sourcing.
        # That is a conflict of interest, not a question of reporting quality.
        # See the source-independence rules in SKILL.md section 4.
        #   ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        #
        # CONFIRMED DEAD: Reuters withdrew public RSS around 2020-2023.
    ],

    # ── COMPETITOR & AI PULSE ───────────────────────────────────────────────
    # Anchors the AI objection script. Needs a tech-first source AND an
    # advisor-trade source: BetaKit alone is tech-first and misses
    # advice-industry and regulatory stories.
    #
    # NOTE: a thin or irrelevant month here is EXPECTED and acceptable. See
    # SKILL.md section 1, "the competitor hook is optional": the AI objection
    # runs on the standing question when nothing material happened, and a
    # forced stale hook is worse than none.
    "Competitor & AI Pulse": [
        # VERIFIED healthy from CI, 150 items.
        ("BetaKit", "https://betakit.com/feed/"),
        # Advisor's Edge, the Canadian advisor trade paper. RSS 2.0.
        # BROWSER-VERIFIED, CI-UNPROVEN: see the Investment Executive note.
        ("Advisor.ca", "https://www.advisor.ca/feed/"),
        # Consumer personal finance, Canadian. Lower tier than the trades,
        # so useful for retail sentiment rather than headline figures.
        # ("MoneySense", "https://www.moneysense.ca/feed/"),

        # DISABLED 2026-09-02 after failing CI preflight with HTTP 403:
        #   investmentexecutive.com/feed/
        # Valid RSS 2.0 from a browser, 403 from a GitHub Actions runner.
        # That is IP-reputation blocking, not TLS fingerprinting, so
        # curl_cffi impersonation cannot recover it. THE LESSON: verifying a
        # feed from a browser does NOT prove the runner can reach it. Only
        # --check-feeds run in CI proves that.
        #
        # CONFIRMED DEAD, do not retry:
        #   investmentexecutive.com/rss-feeds/ -> 404
        #   wealthprofessional.ca/feed         -> 404
    ],
}


# Publisher feeds carry everything the desk published, so relevance is filtered
# here instead of by a search engine. A candidate is kept when its title or
# snippet contains any term. Keep terms broad: a false positive is a wasted
# extraction attempt, while a false negative silently drops the month's story.
CATEGORY_KEYWORDS = {
    "Canada (TSX & Macro)": [
        "tsx", "stock", "equit", "market", "index", "interest rate",
        "policy rate", "inflation", "cpi", "gdp", "recession", "bond",
        "yield", "earnings", "dollar", "loonie", "tariff", "trade",
        "monetary policy", "unemployment", "housing", "mortgage",
        # NOT "bank of canada": on a Bank of Canada feed the institution name
        # matches every item, including the museum's opening hours, so it
        # discriminates nothing. NOT bare "fed": it substring-matches
        # "feed", "federated" and "federal" and would pass anything.
    ],

    "United States (S&P 500 & Fed)": [
        "s&p", "nasdaq", "dow", "stock", "equit", "market", "index",
        "wall street", "interest rate", "fomc", "federal open market",
        "inflation", "cpi", "consumer price", "gdp", "recession",
        "bond", "yield", "treasury", "earnings", "payroll", "unemployment",
        "jobs report", "employment situation", "tariff", "monetary policy",
        # NOT "federal reserve" or bare "fed": same problem as "bank of
        # canada" above. On the Fed's own press feed the institution name
        # matches every item, and "fed" is a substring of "feed".
    ],
    "International & Emerging": [
        "emerging", "international", "global", "china", "india", "japan",
        "europe", "euro", "asia", "latin america", "brazil", "mexico",
        "oil", "commodit", "currency", "geopolit", "imf", "world bank",
        "msci", "export", "supply chain",
    ],
    "Competitor & AI Pulse": [
        # Named competitors and platforms
        "wealthsimple", "questrade", "questwealth", "robinhood",
        "ci direct", "investease", "smartfolio", "nest wealth", "justwealth",
        # The advice business itself
        "robo-advis", "robo advis", "advisor", "adviser",
        "wealth management", "wealth manager", "brokerage",
        "discount broker", "portfolio management", "financial plan",
        "asset management", "mutual fund", "custodian",
        "assets under management",
        # The AI angle
        "artificial intelligence", "agentic", "ai-powered", "ai advisor",
        "fintech", "fin tech",

        # DELIBERATE OMISSIONS, each one a bug that reached the output:
        # "invest"  -> substring of "investors" and "investment", which appear
        #              in nearly every BetaKit funding story. On 2026-09-02 it
        #              alone admitted all three irrelevant Competitor items:
        #              Alberta emissions grants, an agri-lending raise, and a
        #              superconductor pre-seed. Same failure mode as putting
        #              "bank of canada" on a Bank of Canada feed: a term that
        #              matches everything the publication prints discriminates
        #              nothing.
        # "fee"     -> substring of "coffee" and "feed".
        # "ai"      -> substring of "said", "rail", "detail", "campaign".
        # "aum"     -> substring of "trauma".
        # "planner" -> too broad on a general tech feed ("event planner").
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

# Per-category exclusions, applied ON TOP of EXCLUDE_TERMS and to the TITLE
# only. Needed because each publication has its own dominant off-topic beat,
# and an include list broad enough to catch the month's story is always broad
# enough to catch that beat too.
CATEGORY_EXCLUDE = {
    # BetaKit covers ALL Canadian tech. Its cleantech, deep-tech and
    # agri-tech beats are large, well funded, and irrelevant to a wealth
    # brief. Every one of these was in the 2026-09-02 output or one click
    # from it.
    "Competitor & AI Pulse": (
        "emission", "superconductor", "quantum", "hydrogen",
        "carbon capture", "lithium", "drone", "agricultur", "agri-",
        "mining", "biotech", "vaccine", "battery", "solar", "nuclear",
        "defence", "defense", "space", "satellite", "cannabis",
    ),
    # Belt and braces on top of using only non-North-American feeds: if a
    # Canadian or US-domestic story surfaces on BBC or DW it belongs in one of
    # the two country categories, not here. Title-only, so "US tariffs hit
    # European exporters" still qualifies on its European angle.
    "International & Emerging": (
        "canada", "canadian", "ottawa", "alberta", "ontario", "quebec",
        "toronto", "tsx", "loonie",
    ),
    # BBC and DW general-business feeds carry consumer and lifestyle items
    # that match economic terms without being market news.
    "United States (S&P 500 & Fed)": (
        "obituary", "recall notice",
    ),
}

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


def matches_category(item, terms, category=None):
    """Decide whether an item belongs in the category.

    Returns (keep: bool, reason: str). The reason names the term that decided
    it, which is the only practical way to debug this filter: on 2026-09-02
    three irrelevant articles reached the output and identifying the single
    responsible term ("invest") required reconstructing the match by hand
    afterwards. Logging it at gather time makes the next such bug obvious in
    the CI log instead of in the finished PDF.

    Include terms match title OR snippet, so a story with a coy headline still
    qualifies on its summary. Exclude terms match the TITLE ONLY: a real
    market story that mentions "award" or "Canada" in passing in its body must
    not be dropped for it.
    """
    title = str(item.get("title", "")).lower()

    for bad in EXCLUDE_TERMS:
        if bad in title:
            return False, "excluded on %r" % bad
    for bad in CATEGORY_EXCLUDE.get(category or "", ()):
        if bad in title:
            return False, "excluded on %r (category rule)" % bad

    if not terms:
        return True, "no filter"

    hay = "%s %s" % (title, str(item.get("desc", "")).lower())
    for t in terms:
        if t in hay:
            where = "title" if t in title else "snippet"
            return True, "matched %r in %s" % (t, where)
    return False, "no keyword match"


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
                continue          # same story appearing in two feeds
            ok, reason = matches_category(it, terms, category)
            if not ok:
                logging.debug("    drop: %-58s (%s)",
                              it["title"][:58], reason)
                continue
            seen_urls.add(it["url"])
            candidates.append(it)
            kept += 1
            # INFO, not DEBUG: this line is what makes a bad keyword visible
            # in the CI log rather than in the published brief.
            logging.info("    keep: %-58s (%s)", it["title"][:58], reason)
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
    """Validate every configured feed.

    Returns (ok_count, failures, dead_categories).

    Run this in CI before the scraper. A feed that 404s or stops being XML is
    otherwise indistinguishable from a quiet news month: the category just
    comes back thin and nobody knows why.

    SEVERITY IS PER CATEGORY, NOT PER FEED. One publisher going dark while its
    category still has healthy siblings is a warning; the run proceeds and
    loses some diversity. A category with NO healthy feed is different in kind,
    because it falls back to aggregator search and yields uncitable stubs,
    which is the exact failure this module exists to prevent.

    The distinction was added after the first CI preflight failed the whole
    step over one 403 out of nine feeds. A check that cries wolf over a
    survivable fault trains people to ignore it, and this one needs to be
    believed on the month it reports something real.
    """
    if requests is None:
        print("curl_cffi is not installed; run inside the fetcher's env.")
        return 0, [("*", "curl_cffi missing")], list(PUBLISHER_FEEDS)

    failures, ok, dead_categories = [], 0, []
    with requests.Session(impersonate="chrome124") as session:
        session.headers.update({"Accept-Language": "en-CA,en;q=0.9"})
        for category, feeds in PUBLISHER_FEEDS.items():
            if verbose:
                print("\n%s" % category)
            cat_ok = 0
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
                        cat_ok += 1
                        if verbose:
                            print("  ok    %-22s %3d items  %s"
                                  % (publisher, n, urlparse(url).netloc))
                except Exception as exc:                     # noqa: BLE001
                    failures.append((publisher, str(exc)[:80]))
                    if verbose:
                        print("  FAIL  %-22s %s" % (publisher, str(exc)[:60]))
                time.sleep(FEED_DELAY)

            if cat_ok == 0:
                dead_categories.append(category)
                if verbose:
                    print("  >> NO HEALTHY FEED. This category will fall back "
                          "to aggregator search and yield stubs.")
            elif verbose and cat_ok < len(feeds):
                print("  >> %d of %d healthy. Survivable, reduced diversity."
                      % (cat_ok, len(feeds)))

    return ok, failures, dead_categories


def main(argv):
    if "--check-feeds" not in argv:
        print(__doc__.strip().split("\n\n")[0])
        print("\nusage: python3 news_sources.py --check-feeds")
        return 2

    logging.basicConfig(level=logging.WARNING)
    print("=" * 70)
    print("ADVISOR PULSE  |  PUBLISHER FEED PREFLIGHT")
    print("=" * 70)
    ok, failures, dead = check_feeds()
    total = sum(len(v) for v in PUBLISHER_FEEDS.values())
    print("\n" + "-" * 70)
    print("%d of %d feed(s) healthy" % (ok, total))

    if failures:
        print("\nFailed feeds:")
        for pub, why in failures:
            print("  %-22s %s" % (pub, why))
        print("\nA dead feed looks exactly like a quiet news month "
              "downstream. Fix or comment out the URL.")
        print("NOTE: a feed can return valid RSS in a browser and 403 from a "
              "CI runner (datacenter IP reputation). This preflight, run in "
              "CI, is the only verification that counts.")

    # Exit code reflects CATEGORY health, not feed count. See check_feeds.
    if dead:
        print("\nCATEGORIES WITH NO HEALTHY FEED: %s" % ", ".join(dead))
        print("Each will fall back to aggregator search and produce "
              "uncitable stubs. This is the condition worth blocking on.")
        return 1

    if failures:
        print("\nEvery category retains at least one healthy feed. "
              "Proceeding with reduced diversity.")
        return 0

    print("All configured feeds healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
