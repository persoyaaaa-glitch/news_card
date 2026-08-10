"""
article_extract.py
Pulls real body-text paragraphs from a resolved article page, for use as
factual carousel slide content. This is scraped text from the actual
story, not AI-generated - so there's no hallucination/fabrication risk,
but it does mean quality varies by publisher markup.
"""
import re
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://news.google.com/",
}

# Boilerplate/junk paragraphs that show up in <p> tags but aren't real
# story content - subscribe prompts, cookie notices, byline furniture,
# paywall CTAs, and photo-credit lines.
_JUNK_PATTERNS = [
    r"^(subscribe|sign up|newsletter|follow us|advertisement|read more|also read|click here)",
    r"^(copyright|all rights reserved|terms of use|privacy policy)",
    r"^(share this|share on|share via)",
    r"cookies? (to improve|policy|consent)",
    r"^\s*\|\s*$",
    # Paywall / subscription CTAs (these were getting through and showing
    # up as fake "article content" - e.g. "Unlock these with Subscription",
    # "Account subscription benefits alongside Premium Stories...")
    r"\bunlock (this|these|the)\b",
    r"\bpremium stories?\b",
    r"\baccount subscription\b",
    r"\bsubscription benefits?\b",
    r"\balready (a |an )?subscriber\b",
    r"\bsubscribe (now|today) to\b",
    r"\bfree trial\b",
    r"\bpaywall\b",
    r"\blogin to (continue|read)\b",
    r"\bregister (for free|now) to\b",
    # Photo-credit / caption lines (often sit as a plain <p>, not always
    # inside a <figure> we've already stripped)
    r"^\s*\|",
    r"^\s*photo\s*:?\s*credit",
    r"\bphoto\s*credit\s*:",
    r"^\s*\((reuters|afp|pti|ani|ap)\)",
]
_JUNK_RE = re.compile("|".join(_JUNK_PATTERNS), re.IGNORECASE)

# Word-count floor: real sentences have a healthy word count relative to
# their character length. CTA fragments like "Unlock these with
# Subscription" are short and list-like even when they clear min_len.
_MIN_WORDS = 8


def _clean_paragraph(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_article_paragraphs(article_url: str, min_len: int = 60, max_paragraphs: int = 12, timeout: int = 10) -> list[str]:
    """
    Fetch the article page and pull plausible body paragraphs.
    Returns [] if the page can't be fetched or nothing usable is found -
    callers should treat that as "no real content available" and either
    skip the info slides or fall back to just the headline.
    """
    if not article_url or "news.google.com" in article_url:
        return []

    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[article_extract] fetch failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # Strip elements that are never real story content before searching.
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "figure", "form"]):
        tag.decompose()

    # Prefer paragraphs inside an <article> tag if present (most news CMSs
    # use one); fall back to all <p> tags on the page otherwise.
    container = soup.find("article") or soup

    paragraphs = []
    for p in container.find_all("p"):
        text = _clean_paragraph(p.get_text())
        if len(text) < min_len:
            continue
        if _JUNK_RE.search(text):
            continue
        # Skip paragraphs that are mostly a link list / nav text (very few
        # spaces relative to length is a decent heuristic for junk).
        if text.count(" ") < 5:
            continue
        if len(text.split()) < _MIN_WORDS:
            continue
        paragraphs.append(text)
        if len(paragraphs) >= max_paragraphs:
            break

    return paragraphs


def build_slide_texts(paragraphs: list[str], num_slides: int = 3, max_chars: int = 280) -> list[str]:
    """
    Group extracted paragraphs into `num_slides` reasonably-sized chunks
    for the info slides. Each chunk is trimmed to whole sentences so it
    never cuts off mid-word.
    """
    if not paragraphs:
        return []

    full_text = " ".join(paragraphs)
    sentences = re.split(r"(?<=[.!?])\s+", full_text)

    chunks, current = [], ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if len(candidate) > max_chars and current:
            chunks.append(current.strip())
            current = sentence
        else:
            current = candidate
        if len(chunks) >= num_slides:
            break
    if current and len(chunks) < num_slides:
        chunks.append(current.strip())

    return chunks[:num_slides]


def get_carousel_slide_texts(article_url: str, num_slides: int = 3, min_paragraphs: int = 2) -> list[str]:
    """Full pipeline: fetch article -> extract paragraphs -> chunk into slide texts.

    Requires at least `min_paragraphs` real paragraphs before building
    slides - if extraction only turns up a paragraph or two of thin
    content, it returns an empty list rather than building an info-slide
    carousel on weak material. Callers (see hourly_run.py's _build_post)
    treat an empty result as "skip this story" - both the hook and at
    least one description slide are mandatory, so there's no hook-only
    fallback anymore.
    """
    paragraphs = extract_article_paragraphs(article_url)
    if len(paragraphs) < min_paragraphs:
        return []
    return build_slide_texts(paragraphs, num_slides=num_slides)


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python article_extract.py <article_url>")
        sys.exit(1)
    for i, chunk in enumerate(get_carousel_slide_texts(sys.argv[1]), 1):
        print(f"--- slide {i} ---")
        print(chunk)
        print()
