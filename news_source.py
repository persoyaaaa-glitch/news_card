"""
news_source.py
Fetches recent news headlines via Google News RSS (no API key required),
plus a large curated set of direct publisher RSS feeds - both Indian
(domestic) and major international outlets (global) - for maximum
outlet diversity and headline volume.
"""
import calendar
import difflib
import re
import time as _time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError, as_completed

import feedparser
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from lxml import etree as _etree
import warnings

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


# A lot of publisher RSS feeds sit behind Cloudflare/WAF-style bot
# protection that blocks feedparser's default identifying User-Agent
# (e.g. "python-feedparser/...") and silently serves back an HTML
# block/consent page instead of the real XML - which then fails to
# parse as XML with confusing errors ("mismatched tag", "not
# well-formed", etc). A normal browser User-Agent gets past most of
# that, so every feed fetch below routes through this helper instead
# of calling feedparser.parse(url) directly.
_FEED_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_FEED_FETCH_TIMEOUT = 12  # seconds


# Matches "&" that isn't the start of a valid XML entity/char reference
# (&amp; &lt; &gt; &quot; &apos; &#123; &#x1F;) - the single most common
# cause of genuinely broken publisher XML (an un-escaped "&" dropped
# straight into a URL or headline by their feed generator).
_BARE_AMPERSAND = re.compile(rb"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9A-Fa-f]+);)")

# Control characters (other than tab/newline/carriage-return) are illegal
# in XML 1.0 and some feed generators leak them in straight from source
# text (smart-quote mis-encodes, stray bytes, etc), which trips a hard
# "not well-formed" parse failure rather than a soft bozo warning.
_ILLEGAL_XML_CHARS = re.compile(rb"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def _repair_feed_bytes(raw: bytes) -> bytes:
    """
    Best-effort cleanup for the two most common real-world causes of
    "not well-formed"/"mismatched tag" publisher feed errors: bare
    unescaped ampersands and illegal control characters. Not a full XML
    fixer (e.g. won't fix a genuinely unclosed/mismatched tag), but
    those two account for the vast majority of feeds that otherwise
    fail outright.
    """
    raw = _BARE_AMPERSAND.sub(b"&amp;", raw)
    raw = _ILLEGAL_XML_CHARS.sub(b"", raw)
    return raw


def _recover_feed_bytes(raw: bytes):
    """
    Last-resort recovery for feeds broken in ways the regex repair can't
    touch - mismatched/unclosed tags, stray invalid tokens, etc (as
    opposed to just bare "&" or control chars). Runs the bytes through
    lxml's recovering XML parser, which skips/patches the broken bits
    and rebuilds a well-formed tree, then re-serializes that tree back
    to bytes for feedparser. Returns None if lxml can't recover
    anything usable (e.g. truly truncated/non-XML content).
    """
    try:
        parser = _etree.XMLParser(recover=True)
        root = _etree.fromstring(raw, parser=parser)
        if root is None:
            return None
        return _etree.tostring(root)
    except Exception:
        return None


_LINK_OPEN = re.compile(rb"<link>")
_LINK_CLOSE = re.compile(rb"</link>")


def _soup_extract_items(raw: bytes, limit: int) -> list:
    """
    Last-resort extraction for feeds broken badly enough that even
    lxml's recovering parser can't rebuild a usable tree (e.g. more than
    one distinct structural problem in the same document - recovery
    patches around the first one it hits, but can still give up on a
    later, separate issue further down a long feed).

    BeautifulSoup's built-in html.parser backend doesn't parse to
    strict XML/HTML rules at all - it just greedily finds tags by name
    and keeps going regardless of what's broken around them, so it can
    pull <item>/<entry> blocks out of a document that's too damaged for
    any real XML parser to accept. Only used as a plain "salvage what
    we can" step - loses anything feedparser would normally give us
    (structured dates, etc), just enough for title+link+summary.

    RSS's plain-text <link>URL</link> gets specifically renamed to
    <link_>...</link_> first: HTML treats <link> as a void/self-closing
    element (same as <img>/<br>), so html.parser reads
    "<link>https://x</link>" as an empty <link> tag followed by loose
    text and a stray closing tag, silently losing every link. Atom-style
    self-closing <link href="..."/> is unaffected either way.
    """
    try:
        raw = _LINK_OPEN.sub(b"<link_>", raw)
        raw = _LINK_CLOSE.sub(b"</link_>", raw)
        soup = BeautifulSoup(raw, "html.parser")
    except Exception:
        return []

    items = []
    for entry in soup.find_all(["item", "entry"]):
        title_tag = entry.find("title")
        link_tag = entry.find("link_") or entry.find("link")
        title = title_tag.get_text(strip=True) if title_tag else ""
        # RSS <link>URL</link> (renamed to link_ above) vs Atom <link href="URL"/>
        link = ""
        if link_tag:
            link = link_tag.get("href", "").strip() or link_tag.get_text(strip=True)
        if not title or not link:
            continue
        summary_tag = entry.find("description") or entry.find("summary")
        pub_tag = entry.find("pubdate") or entry.find("pubDate") or entry.find("published")
        items.append({
            "title": title,
            "link": link,
            "published": pub_tag.get_text(strip=True) if pub_tag else "",
            "published_parsed": None,  # not worth hand-parsing every date format here
            "summary": summary_tag.get_text(strip=True) if summary_tag else "",
        })
        if len(items) >= limit:
            break
    return items


def _fetch_raw_bytes(url: str):
    """
    Shared timed fetch used by _parse_feed and the soup-extraction
    fallback. Returns bytes, or None (with a debug print) on any
    failure - DNS, timeout, connection refused, HTTP error, etc. Never
    falls back to an unbounded fetch (see _parse_feed's docstring for
    why that's important).
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": _FEED_USER_AGENT, "Accept-Encoding": "identity"},
        )
        with urllib.request.urlopen(req, timeout=_FEED_FETCH_TIMEOUT) as resp:
            return resp.read()
    except Exception as e:
        print(f"[news_source] fetch failed for {url}: {e}")
        return None


def _parse_feed(url: str):
    """
    Fetches `url` with a real browser User-Agent + timeout, then hands
    the raw bytes to feedparser.

    IMPORTANT: every attempt here is time-bounded. Earlier this fell
    back to feedparser.parse(url) - i.e. feedparser doing its own live
    fetch - on ANY exception from the timed urllib fetch, including the
    timeout firing. That fallback has no timeout of its own, so a slow/
    unresponsive server would hit our 12s timeout, get caught, and then
    hang forever on the "fallback" - freezing the entire run (all trusted
    feeds are fetched from one ThreadPoolExecutor batch, so one stuck
    call blocks every other feed and story behind it too). Never call
    feedparser.parse(<url>) with a live URL here - only ever hand it
    bytes we already fetched ourselves under a timeout.

    Also explicitly requests identity (uncompressed) encoding - some
    CDNs gzip/br-compress responses regardless of Accept-Encoding, which
    would otherwise hand raw compressed bytes to feedparser and produce
    exactly the kind of "not well-formed"/"mismatched tag"/"duplicate
    attribute" errors (different each time, as new articles roll through
    the compressed stream) seen from a few publisher feeds.

    If the fetched feed parses but yields zero entries (feedparser gave
    up entirely rather than just setting bozo=True), tries repair passes
    in order, keeping whichever first yields entries:
      1. Regex repair - fixes bare "&" and illegal control characters.
      2. lxml recovering parser on the raw bytes - handles mismatched/
         unclosed tags, stray invalid tokens, duplicate attributes, etc.
      3. lxml recovering parser on the regex-repaired bytes - covers
         feeds broken in more than one of these ways at once.

    Note: the soup-based extraction fallback (last resort, for feeds
    broken in more than one place that even lxml recovery can't fully
    patch) lives in fetch_trusted_feed, not here, since it returns plain
    dicts rather than a feedparser-shaped object.
    """
    raw = _fetch_raw_bytes(url)
    if raw is None:
        # Printed inside _fetch_raw_bytes already. feedparser.parse(b"")
        # reports bozo=False/0 entries, which callers already treat the
        # same as any other unfetchable/unparseable feed.
        return feedparser.parse(b"")

    feed = feedparser.parse(raw)
    if feed.bozo and not feed.entries:
        feed = feedparser.parse(_repair_feed_bytes(raw))
    if feed.bozo and not feed.entries:
        recovered = _recover_feed_bytes(raw)
        if recovered:
            feed = feedparser.parse(recovered)
    if feed.bozo and not feed.entries:
        recovered = _recover_feed_bytes(_repair_feed_bytes(raw))
        if recovered:
            feed = feedparser.parse(recovered)
    return feed


# Words/phrases that strongly signal an outlet itself is treating a story
# as breaking - checked against the headline text.
_BREAKING_KEYWORDS = (
    "breaking", "urgent", "just in", "live updates", "live:", "alert",
    "developing", "watch live", "explosion", "killed in", "dies", "dead",
    "resigns", "arrested", "crash", "attack", "earthquake", "evacuate",
)

# A story counted as breaking if it was published this recently (minutes),
# regardless of keyword match - genuinely fresh news reads as "happening
# now" even without an outlet slapping a "BREAKING" label on it.
_BREAKING_FRESHNESS_MINUTES = 45

# When the same story shows up across this many *different* angles below
# (Google queries + trusted feeds combined), that cross-source repetition
# is itself a signal it's a major/breaking story (small local stories
# don't get picked up by every angle at once).
_BREAKING_CROSS_QUERY_COUNT = 3

# The angles we fan out across for broader coverage than a single feed -
# combines the editorial "top headlines" ordering with explicit breaking
# coverage and a wide set of topical buckets, so a big story that hasn't
# yet risen to the very top of "top headlines" still gets surfaced. This
# same tuple is used for BOTH the India edition and the global/int'l
# edition of Google News (see fetch_best_and_breaking_news), so doubling
# it doubles our topical coverage rather than just our India coverage.
_SURFACE_QUERIES = (
    None,  # top headlines (editorial ordering)
    "breaking news",
    "politics", "business", "economy", "markets",
    "sports", "cricket", "technology", "science",
    "entertainment", "movies",
    "world", "health", "environment", "education", "crime", "defence",
)

# Extra angles only fanned out for the India edition (on top of the
# shared _SURFACE_QUERIES above).
_INDIA_ONLY_QUERIES = ("bollywood", "startup india", "monsoon")

# Extra angles only fanned out for the global/international edition (on
# top of the shared _SURFACE_QUERIES above).
_GLOBAL_ONLY_QUERIES = ("middle east", "ukraine war", "climate change", "artificial intelligence")

# Roughly how "prominent" each kind of angle is, used to weight priority
# scoring (see _priority_score) - top headlines and breaking-news
# searches count for more than a niche topical bucket, and India-edition
# angles are weighted slightly above the equivalent global-edition angle
# since this pipeline's primary audience is India (global stories still
# win outright when they're genuinely breaking/high cross-source pickup).
def _angle_weight(query: str, is_global: bool) -> float:
    if query is None:
        base = 100.0
    elif query == "breaking news":
        base = 95.0
    else:
        base = 55.0
    return base - (10.0 if is_global else 0.0)


TRUSTED_FEED_WEIGHT_INDIA = 55.0
TRUSTED_FEED_WEIGHT_GLOBAL = 45.0

# Direct publisher RSS feeds, fetched alongside the Google News fan-out
# above. These add real outlet diversity instead of routing everything
# through Google's own ranking/selection of what counts as "top" news,
# and have two practical advantages over Google News items:
#   1. The link is already the real publisher URL, not a Google News
#      redirect shell that needs decoding - one less network round trip,
#      and one less thing that breaks if Google changes its interstitial
#      page markup.
#   2. The source name is fixed and known upfront, rather than parsed off
#      a "Headline - Source Name" title string (which occasionally
#      misparses on titles that contain their own " - ").
# Mainstream, editorially-staffed English-language Indian outlets with
# long-standing public RSS feeds, spanning general news, business, and
# sports so the pool isn't just general-news-shaped.
TRUSTED_RSS_FEEDS = (
    ("The Hindu", "https://www.thehindu.com/news/feeder/default.rss"),
    # Hindustan Times removed: both their direct feed (known to emit
    # malformed XML) and this Feedburner-hosted mirror (feeds.
    # hindustantimes.com) have now gone dead - the mirror's hostname no
    # longer resolves at all (DNS failure, not a parse error), so
    # there's currently no working HT feed to point at. HT coverage
    # still flows in fine through the Google News India edition/topical
    # buckets in the meantime. If you find their current working RSS
    # URL, send it over and it's a one-line add back into this tuple.
    ("Times of India", "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"),
    ("NDTV", "http://feeds.feedburner.com/ndtvnews-top-stories"),
    ("Indian Express", "https://indianexpress.com/section/india/feed/"),
    ("India Today", "https://www.indiatoday.in/rss/1206584"),
    ("LiveMint", "https://www.livemint.com/rss/news"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/latestnews.xml"),
    ("Business Standard", "https://www.business-standard.com/rss/latest.rss"),
    ("News18", "https://www.news18.com/rss/india.xml"),
    ("Deccan Herald", "https://prod-qt-images.s3.amazonaws.com/production/deccanherald/feed.xml"),
    ("The Print", "https://theprint.in/feed/"),
    ("Scroll.in", "https://scroll.in/feed"),
    ("Firstpost", "https://www.firstpost.com/rss/india.xml"),
    ("Zee News", "https://zeenews.india.com/rss/india-national-news.xml"),
)

# Major international outlets with long-standing public RSS feeds, for
# genuinely global/international coverage that isn't India-specific
# (world affairs, US/Europe/Middle East politics, global business/tech).
# Only mainstream, editorially-staffed English-language outlets.
GLOBAL_RSS_FEEDS = (
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("The Guardian World", "https://www.theguardian.com/world/rss"),
    ("NYT World", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
    ("Washington Post World", "https://www.washingtonpost.com/rss/world"),
    ("CNBC World", "https://www.cnbc.com/id/100727362/device/rss/rss.html"),
    ("DW World", "https://rss.dw.com/rdf/rss-en-world"),
    ("France24", "https://www.france24.com/en/rss"),
    ("ABC News Australia", "https://www.abc.net.au/news/feed/51120/rss.xml"),
)


def _normalize_for_match(title: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in title).split())


def _is_fresh(published_parsed, minutes: int = _BREAKING_FRESHNESS_MINUTES) -> bool:
    if not published_parsed:
        return False
    try:
        published_epoch = calendar.timegm(published_parsed)
    except Exception:
        return False
    return (_time.time() - published_epoch) <= minutes * 60


def _looks_breaking_by_keyword(title: str) -> bool:
    lowered = title.lower()
    return any(kw in lowered for kw in _BREAKING_KEYWORDS)


def _priority_score(item: dict, hit_count: int, weight: float, position: int) -> float:
    """
    Composite score used to literally rank every story from best (highest
    score, priority_rank=1) down to least important, instead of just
    splitting everything into two breaking/non-breaking buckets. Higher
    is better. Combines:
      - is it breaking (keyword/freshness/cross-source) - the single
        biggest signal a story matters right now
      - how many independent angles (Google queries, India + global,
        plus trusted feeds) picked up the same story - wider pickup =
        bigger story
      - how fresh it is - more recent generally beats older, decaying
        to ~0 after a few hours rather than a hard cutoff
      - how prominent the angle that first/best surfaced it is (top
        headlines and breaking-news searches count for more than a
        niche topical bucket - see _angle_weight), and how early it sat
        within that angle's own results
    """
    score = 0.0

    if item["is_breaking"]:
        score += 500.0

    # Cross-source pickup: each additional independent angle that
    # surfaced this same story is a strong "this is a big story" signal.
    # With far more angles fanned out now (India + global Google News,
    # India + global trusted feeds), this naturally rewards stories big
    # enough to appear everywhere.
    score += (hit_count - 1) * 40.0

    # Freshness: full bonus for something inside the breaking-freshness
    # window, decaying to ~0 over the next few hours - not a cliff.
    published_parsed = item.get("published_parsed")
    if published_parsed:
        try:
            age_minutes = (_time.time() - calendar.timegm(published_parsed)) / 60.0
            score += max(0.0, 200.0 - age_minutes)  # ~0 after ~3.3 hours old
        except Exception:
            pass

    # Editorial prominence: the best angle that surfaced this story
    # (its weight, see _angle_weight/TRUSTED_FEED_WEIGHT_*) plus an
    # earlier position within that angle's own results both count for
    # more than something buried near the bottom of a niche bucket.
    score += weight - (position * 1.5)

    return score


def fetch_news(query: str, limit: int = 10, lang: str = "en", country: str = "IN"):
    """
    Fetch recent news items matching `query`.

    Returns a list of dicts: {title, link, published, published_parsed, source}
    """
    encoded_query = urllib.parse.quote(query)
    url = (
        f"https://news.google.com/rss/search?q={encoded_query}"
        f"&hl={lang}-{country}&gl={country}&ceid={country}:{lang}"
    )

    feed = _parse_feed(url)
    items = []
    for entry in feed.entries[:limit]:
        # Google News RSS titles look like "Headline - Source Name"
        raw_title = entry.get("title", "")
        source = ""
        headline = raw_title
        if " - " in raw_title:
            headline, source = raw_title.rsplit(" - ", 1)

        items.append({
            "title": headline.strip(),
            "source": source.strip() or entry.get("source", {}).get("title", ""),
            "link": entry.get("link", ""),
            "published": entry.get("published", ""),
            "published_parsed": entry.get("published_parsed"),
            "summary": entry.get("summary", ""),
        })
    return items


def fetch_top_headlines(lang: str = "en", country: str = "IN", limit: int = 10):
    """Fetch general top headlines (no specific query)."""
    url = f"https://news.google.com/rss?hl={lang}-{country}&gl={country}&ceid={country}:{lang}"
    feed = _parse_feed(url)
    items = []
    for entry in feed.entries[:limit]:
        raw_title = entry.get("title", "")
        source = ""
        headline = raw_title
        if " - " in raw_title:
            headline, source = raw_title.rsplit(" - ", 1)
        items.append({
            "title": headline.strip(),
            "source": source.strip(),
            "link": entry.get("link", ""),
            "published": entry.get("published", ""),
            "published_parsed": entry.get("published_parsed"),
            "summary": entry.get("summary", ""),
        })
    return items


def fetch_trusted_feed(source_name: str, feed_url: str, limit: int = 15) -> list:
    """
    Fetch recent items directly from a publisher's own RSS feed, as
    opposed to Google News. Returns the same item shape as fetch_news/
    fetch_top_headlines ({title, source, link, published,
    published_parsed, summary}) so it can be merged straight into the
    same fan-out/dedup pipeline as the Google queries.

    Unlike Google News items, `link` here is already the real article
    URL and `source` is known upfront (not parsed off the title), so
    downstream resolution/extraction has one less step to worry about.

    Returns [] (and prints a debug line) if the feed can't be fetched or
    parsed at all - one bad/renamed feed URL shouldn't take down the
    rest of the pipeline.
    """
    try:
        feed = _parse_feed(feed_url)
        if feed.bozo and not feed.entries:
            # bozo=True just means "didn't parse as strict XML" - lots of
            # real-world feeds trip this but still yield usable entries,
            # so only treat it as a failure if we got nothing at all.
            raise ValueError(feed.get("bozo_exception", "unparseable feed"))
        entries = feed.entries
    except Exception as e:
        # Every XML-based repair tier in _parse_feed already failed - last
        # resort is BeautifulSoup's non-XML html.parser, which finds
        # <item>/<entry> tags regardless of how broken everything around
        # them is. Costs one extra fetch (only reached this rarely, when
        # a feed is broken in more than one distinct way), but recovers
        # real stories that would otherwise just get dropped.
        raw = _fetch_raw_bytes(feed_url)
        entries = _soup_extract_items(raw, limit) if raw else []
        if not entries:
            print(f"[news_source] trusted feed {source_name!r} ({feed_url}) failed: {e}")
            return []
        print(f"[news_source] trusted feed {source_name!r} recovered {len(entries)} item(s) via soup fallback after: {e}")

    items = []
    for entry in entries[:limit]:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        if not title or not link:
            continue
        items.append({
            "title": title,
            "source": source_name,
            "link": link,
            "published": entry.get("published", ""),
            "published_parsed": entry.get("published_parsed"),
            "summary": entry.get("summary", ""),
        })
    return items


def fetch_best_and_breaking_news(
    country: str = "IN",
    lang: str = "en",
    limit_per_query: int = 20,
    include_global: bool = True,
    max_workers: int = 16,
) -> list:
    """
    Maximum-coverage news surfacing: fans out across a large number of
    independent angles in parallel (via a thread pool - all of this is
    network-bound RSS/HTTP fetching, so threads give a big wall-clock
    win with no added complexity) and merges the results into one
    de-duplicated, ranked list. Angles fanned out to:

      - India edition Google News: top headlines, "breaking news", and
        ~16 topical buckets (politics/business/economy/markets/sports/
        cricket/tech/science/entertainment/movies/world/health/
        environment/education/crime/defence) PLUS India-only extras
        (bollywood/startup india/monsoon)
      - Global/international edition Google News (en-US): the same
        shared topical buckets PLUS global-only extras (middle east/
        ukraine war/climate change/artificial intelligence) - only
        fanned out when include_global=True
      - TRUSTED_RSS_FEEDS: ~14 direct Indian publisher feeds
      - GLOBAL_RSS_FEEDS: ~11 direct international publisher feeds -
        only fanned out when include_global=True

    That's on the order of 60-70 independent angles by default, versus
    the previous ~14 - built specifically so a run can go wide (many
    more candidate stories per pull, domestic AND global) rather than
    just fast. Any single angle failing (dead feed, timeout, bad XML)
    is caught and skipped - it never takes down the rest of the pull.

    Then:
      - de-duplicates near-identical headlines across every angle (same
        story, slightly different wording/outlet) using fuzzy title
        matching, keeping the first (best-ranked) copy seen
      - flags each item as is_breaking=True if it matches breaking
        keywords, was published within the last ~45 minutes, or showed
        up independently across several angles above (cross-source
        repetition = big story - now checked across a much wider set of
        angles, domestic and global alike)
      - scores every story with a composite priority score (see
        _priority_score) and sorts the whole list by it, so the result
        is a real best-to-worst ranking rather than just a breaking/
        non-breaking split - the story at index 0 (priority_rank=1) is
        literally the single best story in the batch

    include_global: fan out to the global/international Google News
    edition and GLOBAL_RSS_FEEDS as well as the India-specific angles.
    Set False to restrict the pull to India-only coverage (old
    behavior).

    Returns a list of dicts, sorted best-first, each with added keys:
      - is_breaking (bool)
      - priority_score (float) - the raw composite score, mainly useful
        for debugging/tuning
      - priority_rank (int) - 1 = the best story in the batch, 2 = next
        best, etc. - every returned story gets a rank, not just the top
        one
    """
    # Build the full list of fetch jobs up front: (label, weight, fetch_fn)
    # so they can all be dispatched to the thread pool together.
    jobs = []  # list of (label, weight, callable_returning_items)

    india_queries = _SURFACE_QUERIES + _INDIA_ONLY_QUERIES
    for query in india_queries:
        weight = _angle_weight(query, is_global=False)
        fn = (
            (lambda q=None: fetch_top_headlines(lang=lang, country=country, limit=limit_per_query))
            if query is None
            else (lambda q=query: fetch_news(q, limit=limit_per_query, lang=lang, country=country))
        )
        jobs.append((f"IN google:{query}", weight, fn))

    if country == "IN" and lang == "en":
        for source_name, feed_url in TRUSTED_RSS_FEEDS:
            jobs.append((
                f"IN feed:{source_name}", TRUSTED_FEED_WEIGHT_INDIA,
                lambda s=source_name, u=feed_url: fetch_trusted_feed(s, u, limit=limit_per_query),
            ))

    if include_global:
        global_queries = _SURFACE_QUERIES + _GLOBAL_ONLY_QUERIES
        for query in global_queries:
            weight = _angle_weight(query, is_global=True)
            fn = (
                (lambda q=None: fetch_top_headlines(lang="en", country="US", limit=limit_per_query))
                if query is None
                else (lambda q=query: fetch_news(q, limit=limit_per_query, lang="en", country="US"))
            )
            jobs.append((f"GLOBAL google:{query}", weight, fn))

        for source_name, feed_url in GLOBAL_RSS_FEEDS:
            jobs.append((
                f"GLOBAL feed:{source_name}", TRUSTED_FEED_WEIGHT_GLOBAL,
                lambda s=source_name, u=feed_url: fetch_trusted_feed(s, u, limit=limit_per_query),
            ))

    # Dispatch every job in parallel - this is the piece that makes ~65
    # angles practical without the run taking minutes: each angle is one
    # blocking HTTP+XML-parse call, so a thread pool overlaps their wait
    # time instead of paying it 65 times in sequence.
    #
    # Bounded defense-in-depth: every individual fetch already has its
    # own timeout (_FEED_FETCH_TIMEOUT / per-request timeouts), but a
    # plain `with ThreadPoolExecutor(...)` still blocks on exit until
    # every submitted job finishes, however long that takes - so one
    # unexpectedly hung thread (e.g. a DNS resolve that itself hangs
    # past the socket timeout) would still freeze the whole run. Instead:
    # give as_completed() itself a hard overall deadline, and shut the
    # pool down without waiting for stragglers - any job still running
    # past the deadline is simply dropped from this batch rather than
    # blocking it.
    fetched = []  # list of (weight, items)
    pool = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_to_job = {pool.submit(fn): (label, weight) for label, weight, fn in jobs}
        overall_deadline = _FEED_FETCH_TIMEOUT + 20  # generous cushion over one feed's own timeout
        try:
            for future in as_completed(future_to_job, timeout=overall_deadline):
                label, weight = future_to_job[future]
                try:
                    items = future.result()
                except Exception as e:
                    print(f"[news_source] angle {label!r} failed: {e}")
                    continue
                fetched.append((weight, items))
        except _FutureTimeoutError:
            stuck = [label for f, (label, _) in future_to_job.items() if not f.done()]
            print(f"[news_source] fan-out hit {overall_deadline}s deadline, dropping {len(stuck)} still-running angle(s): {stuck}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    seen_links = set()
    seen_normalized = []       # normalized titles, for fuzzy dedup
    combined = []              # one entry per unique story
    cross_query_hits = []      # count of which angles matched each story (parallel to combined)
    best_ranks = []            # best-seen (-weight, position) per story (parallel to combined)
    # Blocking index (first normalized word -> indices into combined) so
    # fuzzy-dedup only compares each new item against plausible matches
    # instead of every story seen so far - needed to keep dedup fast now
    # that a single pull can easily surface 1000+ raw items.
    first_word_index = {}

    def _ingest(items: list, weight: float) -> None:
        """Merge a batch of items (from one angle) into combined/dedup state."""
        for position, item in enumerate(items):
            title, link = item.get("title", "").strip(), item.get("link", "").strip()
            if not title or not link:
                continue

            normalized = _normalize_for_match(title)
            if link in seen_links:
                continue

            first_word = normalized.split(" ", 1)[0] if normalized else ""
            candidate_idxs = first_word_index.get(first_word, ())

            match_idx = None
            for idx in candidate_idxs:
                if difflib.SequenceMatcher(None, normalized, seen_normalized[idx]).ratio() >= 0.82:
                    match_idx = idx
                    break

            if match_idx is not None:
                cross_query_hits[match_idx] += 1
                # If this same story also shows up more prominently in a
                # different/better angle (e.g. it's on "breaking news"
                # too, not just buried in a niche topical bucket), let
                # the better placement win for scoring purposes.
                candidate_rank = (-weight, position)
                if candidate_rank < best_ranks[match_idx]:
                    best_ranks[match_idx] = candidate_rank
                continue

            seen_links.add(link)
            new_idx = len(combined)
            seen_normalized.append(normalized)
            combined.append(item)
            cross_query_hits.append(1)
            best_ranks.append((-weight, position))
            first_word_index.setdefault(first_word, []).append(new_idx)

    for weight, items in fetched:
        _ingest(items, weight)

    for item, hit_count, (neg_weight, position) in zip(combined, cross_query_hits, best_ranks):
        item["is_breaking"] = (
            _looks_breaking_by_keyword(item["title"])
            or _is_fresh(item.get("published_parsed"))
            or hit_count >= _BREAKING_CROSS_QUERY_COUNT
        )
        item["priority_score"] = _priority_score(item, hit_count, -neg_weight, position)

    combined.sort(key=lambda it: it["priority_score"], reverse=True)
    for rank, item in enumerate(combined, start=1):
        item["priority_rank"] = rank

    return combined


if __name__ == "__main__":
    results = fetch_best_and_breaking_news(limit_per_query=15)
    print(f"\n{len(results)} unique stories after dedup:\n")
    for r in results[:25]:
        flag = "[BREAKING] " if r["is_breaking"] else ""
        print(f"#{r['priority_rank']:<3} {flag}{r['title']} | {r['source']} | score={r['priority_score']:.0f}")
