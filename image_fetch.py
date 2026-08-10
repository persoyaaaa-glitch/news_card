"""
image_fetch.py
Resolves a Google News redirect link to the real article, extracts the
og:image meta tag, and downloads the image as a .png.
"""
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


def get_og_image_url(article_url: str, timeout: int = 10) -> str | None:
    """Fetch the article page and pull the og:image meta tag."""
    if "news.google.com" in article_url:
        # We failed to resolve to a real publisher URL - don't even try,
        # since this would just scrape Google's own placeholder image.
        return None
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=timeout)
        soup = BeautifulSoup(resp.text, "html.parser")

        for prop in ("og:image", "og:image:secure_url", "twitter:image"):
            tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
            if tag and tag.get("content"):
                image_url = tag["content"]
                if any(blocked in image_url for blocked in BLOCKED_IMAGE_HOSTS):
                    continue  # skip Google's own logo/placeholder images
                return image_url
        return None
    except requests.RequestException:
        return None


def download_image(image_url: str, out_path: str, timeout: int = 15) -> bool:
    """Download an image URL and save it as PNG at out_path."""
    try:
        resp = requests.get(image_url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content)).convert("RGB")
        img.save(out_path, "PNG")
        return True
    except Exception as e:
        print(f"[image_fetch] failed to download image: {e}")
        return False


def get_article_image(google_news_link: str, out_path: str) -> bool:
    """
    Full pipeline: resolve redirect -> find og:image -> download as PNG.
    Returns True on success.
    """
    article_url = resolve_article_url(google_news_link)
    return get_article_image_from_resolved_url(article_url, out_path)


def get_article_image_from_resolved_url(article_url: str, out_path: str) -> bool:
    """Same as get_article_image, but skips the redirect-resolution step
    when the caller already has the resolved article URL (e.g. it was
    also needed for article text extraction, so resolving it twice would
    just be a wasted request)."""
    image_url = get_og_image_url(article_url)
    if not image_url:
        return False
    return download_image(image_url, out_path)


if __name__ == "__main__":
    # quick manual test placeholder
    print("Run via main.py for a full pipeline test.")
