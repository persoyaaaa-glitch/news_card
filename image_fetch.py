"""
image_fetch.py
Resolves a Google News redirect link to the real article, extracts the
best usable article photo, and downloads it as a .png.

Also validates every candidate image before accepting it - og:image
(and friends) frequently point at a source's masthead logo, a generic
"BREAKING NEWS" banner graphic (common on live-blog/rolling-update
articles), or a tiny thumbnail that looks blurry/zoomed once stretched
to fill a card. See is_usable_article_image() for the checks, and
CANDIDATE_META_PROPS/_iter_candidate_image_urls() for how multiple
candidates on the same page are tried in order before giving up.
"""
import re
import requests
from bs4 import BeautifulSoup
from PIL import Image
from io import BytesIO

from googlenewsdecoder import gnewsdecoder
from googlenewsdecoder.decoderv1 import decode_google_news_url as _decode_offline

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://news.google.com/",
}


def resolve_article_url(google_news_link: str, timeout: int = 10) -> str:
    """
    Resolve a Google News RSS link to the real publisher article URL.

    Google no longer plain-HTTP-redirects these - it serves an
    interstitial page whose real target must be decoded. We used to hand
    -roll that decode (scraping a `c-wiz > div` node + a raw batchexecute
    POST), but Google tweaks the interstitial markup often enough that
    the hand-rolled selector would silently stop matching - and when it
    did, EVERYTHING downstream broke at once: no og:image (falls back to
    a generated background = the "ILLUSTRATIVE IMAGE" label), no article
    paragraphs (skips the story entirely - a description slide is
    mandatory, there's no hook-only fallback), and no article text to
    ground the caption AI call in (falls back to a templated caption
    with no hashtags/song).

    So this now uses `googlenewsdecoder`, a small actively-maintained
    package dedicated to this one job, with two layers:

      1. Fast/offline: some Google News links encode the real URL
         directly in their base64 payload, decodable with no network
         round-trip at all.
      2. Networked: fetches the interstitial, pulls the signature +
         timestamp Google requires, and calls the internal batchexecute
         endpoint - same idea as before, but it also retries against the
         older `/rss/articles/...` page shape if the newer one fails.

    Falls back to a plain redirect-follow as a last resort, and logs
    which stage failed so it's easy to debug from the terminal.
    """
    if "news.google.com" not in google_news_link:
        return google_news_link

    try:
        offline = _decode_offline(google_news_link)
        if offline and offline.startswith("http") and "news.google.com" not in offline:
            return offline
    except Exception as e:
        print(f"  [debug] offline google-news decode failed: {e}")

    try:
        result = gnewsdecoder(google_news_link)
        if result.get("status") and (result.get("decoded_url") or "").startswith("http"):
            return result["decoded_url"]
        print(f"  [debug] gnewsdecoder failed: {result.get('message')}")
    except Exception as e:
        print(f"  [debug] gnewsdecoder raised: {e}")

    try:
        resp = requests.get(google_news_link, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if "news.google.com" not in resp.url:
            return resp.url
        return resp.url  # give up, return whatever we had
    except requests.RequestException as e:
        print(f"  [debug] plain redirect fetch failed: {e}")
        return google_news_link


BLOCKED_IMAGE_HOSTS = ("gstatic.com", "google.com", "googleusercontent.com/news")

# Filename/path keywords that overwhelmingly mean "this isn't a real
# article photo" - a source's own masthead/social-share logo, a generic
# rolling-update/live-blog banner ("BREAKING NEWS" style graphics),
# or an explicit CMS placeholder/fallback image. Checked against the
# lowercased URL before ever downloading anything, so a bad candidate
# costs nothing and the next candidate (see CANDIDATE_META_PROPS) gets
# a chance instead.
BLOCKED_URL_KEYWORDS = (
    "logo", "placeholder", "default-image", "default_image", "fallback",
    "og-default", "opengraph-default", "breaking-news", "breakingnews",
    "live-blog", "liveblog", "watermark", "masthead", "favicon",
    "apple-touch-icon", "sprite",
)

# Tried in this order for every page; the first candidate that both
# exists AND passes is_usable_article_image() wins. Real articles
# usually only differ in which of these a given CMS happens to
# populate - trying all of them (instead of just the first match, as
# before) means a bad og:image no longer sinks the whole story if
# twitter:image or a same-page <img> would have worked.
CANDIDATE_META_PROPS = ("og:image", "og:image:secure_url", "twitter:image")

# Real news-photo og:images are essentially always well over this on
# both axes; anything smaller is almost always a logo, icon, or a
# thumbnail that would look visibly soft/blurry once stretched to fill
# a full-bleed card.
MIN_IMAGE_WIDTH = 400
MIN_IMAGE_HEIGHT = 300

# Outside this range the image is a banner/strip shape (masthead logos,
# ad banners) or an unnaturally tall crop - not a normal photo.
MIN_ASPECT_RATIO = 0.35   # taller than ~1:2.9
MAX_ASPECT_RATIO = 3.2    # wider than ~3.2:1

# Below this, an image is almost always a flat-color graphic: a logo on
# a solid background, or a "BREAKING NEWS" template banner (bold text +
# a big flat color field). Real photographs - even fairly uniform ones
# like a plain sky - virtually always score well below this. Measured
# as the fraction of sampled pixels that fall within a small distance
# of the single most common color - a coverage-based metric, not raw
# variance: a colored shape/logo on a solid background can have HIGH
# variance (white vs. a saturated brand color is a huge per-channel
# jump) while still being overwhelmingly one flat color by pixel count,
# which is what actually distinguishes a graphic from a photo.
MAX_DOMINANT_COLOR_COVERAGE = 0.55


def _dominant_color_coverage(img: "Image.Image") -> float:
    """Fraction of sampled pixels within a small color distance of the
    single most common (quantized) color. Downsamples and buckets
    colors into a coarse palette first, both for speed and so that
    JPEG noise/gradients within what's visually "one color" don't
    artificially depress the count."""
    sample = img.convert("RGB").resize((80, 80))
    # Quantize to a small adaptive palette, then count how many pixels
    # map to the single largest palette entry.
    quantized = sample.quantize(colors=16, method=Image.FASTOCTREE)
    counts = quantized.getcolors(maxcolors=16)  # [(count, palette_index), ...]
    if not counts:
        return 0.0
    total = sum(c for c, _ in counts)
    dominant = max(c for c, _ in counts)
    return dominant / total


def is_usable_article_image(img: "Image.Image", source_desc: str = "") -> tuple[bool, str]:
    """
    Heuristic pass/fail on an already-downloaded/decoded image, no
    external API calls. Returns (is_usable, reason) - reason is always
    populated (even on a pass, e.g. "ok") so callers can log exactly
    why a candidate was rejected instead of just "no usable image".

    Three checks, cheapest/most decisive first:
      1. Resolution floor - rejects logos/icons/thumbnails.
      2. Aspect ratio band - rejects banner strips and masthead logos.
      3. Dominant-color-coverage ceiling - rejects flat-color graphics
         (a logo on a solid background, or a template "BREAKING NEWS"
         banner), which pass the first two checks fine since they're
         often published at full photo-sized dimensions. NOTE: this is
         still a pixel-statistics heuristic, not real image
         understanding - it reliably catches "mostly one flat color"
         graphics (the large majority of real logos/banners in
         practice), but a busy, multi-color infographic or a
         collage-style banner could still slip through. There's no
         vision-model call in this pipeline to catch those; treat this
         as a strong filter, not a guarantee.
    """
    w, h = img.size
    if w < MIN_IMAGE_WIDTH or h < MIN_IMAGE_HEIGHT:
        return False, f"too small ({w}x{h}, need >= {MIN_IMAGE_WIDTH}x{MIN_IMAGE_HEIGHT})"

    aspect = w / h
    if aspect < MIN_ASPECT_RATIO or aspect > MAX_ASPECT_RATIO:
        return False, f"bad aspect ratio ({w}x{h} = {aspect:.2f})"

    coverage = _dominant_color_coverage(img)
    if coverage > MAX_DOMINANT_COLOR_COVERAGE:
        return False, f"looks like a flat graphic/logo, not a photo ({coverage:.0%} one dominant color)"

    return True, "ok"


def _url_looks_generic(image_url: str) -> bool:
    lowered = image_url.lower()
    return any(kw in lowered for kw in BLOCKED_URL_KEYWORDS)


def _iter_candidate_image_urls(soup: "BeautifulSoup"):
    """Yields every plausible article-image URL on the page, in
    priority order, deduplicated. Meta tags first (cheapest/most
    reliable when populated), then an image_src link tag, then finally
    the largest same-page <img> with explicit width/height attributes
    (a last-resort fallback for pages with no usable meta image at
    all - many CMSs still tag the actual hero image with size
    attributes even when they skip the OG tags)."""
    seen = set()

    for prop in CANDIDATE_META_PROPS:
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            url = tag["content"].strip()
            if url and url not in seen:
                seen.add(url)
                yield url

    link_tag = soup.find("link", rel="image_src")
    if link_tag and link_tag.get("href"):
        url = link_tag["href"].strip()
        if url and url not in seen:
            seen.add(url)
            yield url

    # Last resort: the largest <img> in the page body that declares its
    # own dimensions big enough to plausibly be a hero photo (skips the
    # width/height check entirely for anything without both attributes,
    # since an unsized img is usually an icon/spacer in practice).
    sized_imgs = []
    for img_tag in soup.find_all("img"):
        src = img_tag.get("src") or img_tag.get("data-src")
        if not src or src in seen:
            continue
        try:
            w, h = int(img_tag.get("width", 0)), int(img_tag.get("height", 0))
        except (TypeError, ValueError):
            continue
        if w >= MIN_IMAGE_WIDTH and h >= MIN_IMAGE_HEIGHT:
            sized_imgs.append((w * h, src))
    for _, src in sorted(sized_imgs, reverse=True):
        if src not in seen:
            seen.add(src)
            yield src


def get_og_image_url(article_url: str, timeout: int = 10) -> str | None:
    """
    Fetch the article page and return the first candidate image URL
    that looks non-generic by its filename/path alone (see
    BLOCKED_URL_KEYWORDS) - actual pixel-content validation (resolution/
    aspect/flatness) happens after download, in download_image(), since
    that requires the bytes. Kept as a separate function (rather than
    folded into download_image) because article_extract.py and other
    callers may want just the URL.
    """
    if "news.google.com" in article_url:
        # We failed to resolve to a real publisher URL - don't even try,
        # since this would just scrape Google's own placeholder image.
        return None
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=timeout)
        soup = BeautifulSoup(resp.text, "html.parser")

        for image_url in _iter_candidate_image_urls(soup):
            if any(blocked in image_url for blocked in BLOCKED_IMAGE_HOSTS):
                continue  # Google's own logo/placeholder images
            if _url_looks_generic(image_url):
                continue  # masthead logo / breaking-news banner / CMS fallback, by filename
            return image_url
        return None
    except requests.RequestException:
        return None


def _get_all_candidate_urls(article_url: str, timeout: int = 10) -> list:
    """Like get_og_image_url, but returns every non-generic-by-filename
    candidate (not just the first) so download_image can fall through
    to the next one if the first fails PIXEL validation after
    downloading. Separate function so get_og_image_url's simple
    single-URL contract stays intact for any other caller."""
    if "news.google.com" in article_url:
        return []
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=timeout)
        soup = BeautifulSoup(resp.text, "html.parser")
        return [
            url for url in _iter_candidate_image_urls(soup)
            if not any(blocked in url for blocked in BLOCKED_IMAGE_HOSTS)
            and not _url_looks_generic(url)
        ]
    except requests.RequestException:
        return []


def download_image(image_url: str, out_path: str, timeout: int = 15) -> bool:
    """Download an image URL, validate it looks like a real article
    photo (see is_usable_article_image), and save it as PNG at
    out_path. Returns False (without saving) if the download fails OR
    the image fails validation - callers should treat both the same
    way (try the next candidate / skip the story)."""
    try:
        resp = requests.get(image_url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content)).convert("RGB")
    except Exception as e:
        print(f"[image_fetch] failed to download image: {e}")
        return False

    ok, reason = is_usable_article_image(img, image_url)
    if not ok:
        print(f"[image_fetch] rejected candidate image ({reason}): {image_url[:100]}")
        return False

    try:
        img.save(out_path, "PNG")
        return True
    except Exception as e:
        print(f"[image_fetch] failed to save image: {e}")
        return False


def get_article_image(google_news_link: str, out_path: str) -> bool:
    """
    Full pipeline: resolve redirect -> find best usable image -> download as PNG.
    Returns True on success.
    """
    article_url = resolve_article_url(google_news_link)
    return get_article_image_from_resolved_url(article_url, out_path)


def get_article_image_from_resolved_url(article_url: str, out_path: str) -> bool:
    """Same as get_article_image, but skips the redirect-resolution step
    when the caller already has the resolved article URL (e.g. it was
    also needed for article text extraction, so resolving it twice would
    just be a wasted request).

    Tries every non-generic-by-filename candidate on the page in order
    (see _iter_candidate_image_urls) and downloads the first one that
    also passes pixel-level validation (resolution/aspect/flatness -
    see is_usable_article_image), instead of committing to a single
    og:image and giving up if it's a logo or a low-quality thumbnail.
    """
    for image_url in _get_all_candidate_urls(article_url):
        if download_image(image_url, out_path):
            return True
    return False


if __name__ == "__main__":
    # quick manual test placeholder
    print("Run via main.py for a full pipeline test.")
