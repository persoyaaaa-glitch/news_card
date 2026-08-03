"""
batch_preview.py
Generates a full hourly batch WITHOUT posting anything - purely for
reviewing output before wiring it back into the live posting flow.

For each of 10 news stories, builds exactly 2 images:
  Slide A - the eye-catching hook: real photo fetched from the article,
            composited into the existing gradient/headline card layout.
  Slide B - a true black-and-white version of that same photo, with the
            real extracted article detail text.

10 stories x 2 slides = 20 images total, saved to preview_output/.

Does NOT: upload to Supabase, post to Instagram, or mark anything as
posted. Safe to run repeatedly while iterating on the look.
"""
import os
import random
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from news_source import fetch_top_headlines
from image_fetch import get_article_image_from_resolved_url, resolve_article_url
from article_extract import get_carousel_slide_texts, extract_article_paragraphs
from card_generator import build_carousel, HEADLINE_THEMES
from ai_text import generate_hook_and_detail
import re

PREVIEW_DIR = "preview_output"
TMP_DIR = "tmp_images"
TARGET_STORY_COUNT = 10          # 10 stories x 2 slides = 20 images
CANDIDATE_POOL_SIZE = 40         # fetch a bigger pool since some articles will be skipped

CATEGORY_KEYWORDS = {
    "POLITICS": ["election", "minister", "parliament", "government", "modi", "bjp", "congress party", "policy"],
    "BUSINESS": ["market", "stock", "economy", "rupee", "ipo", "startup", "rbi", "inflation", "sensex"],
    "SPORTS": ["cricket", "ipl", "football", "olympics", "match", "tournament", "player", "world cup"],
    "TECH": ["ai", "tech", "app", "smartphone", "software", "cyber", "data", "google", "meta"],
    "ENTERTAINMENT": ["bollywood", "film", "movie", "actor", "actress", "box office", "celebrity"],
    "WORLD": ["united states", "china", "pakistan", "united nations", "war", "president", "trump", "international"],
}


def slugify(text: str, max_len: int = 40) -> str:
    keep = "".join(c if c.isalnum() or c == " " else "" for c in text)
    return "_".join(keep.lower().split())[:max_len]


def detect_category(headline: str) -> str:
    lowered = headline.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", lowered):
                return category
    return "NEWS"


def run():
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    os.makedirs(TMP_DIR, exist_ok=True)

    print(f"[{datetime.now().isoformat()}] Fetching a pool of headlines...")
    articles = fetch_top_headlines(country="IN", limit=CANDIDATE_POOL_SIZE)
    if not articles:
        print("No articles returned. Exiting.")
        return

    theme = random.choice(HEADLINE_THEMES)
    print(f"Theme for this batch: {theme['name']} (logo: {os.path.basename(theme['logo'])})")

    built = 0
    skipped = []

    for article in articles:
        if built >= TARGET_STORY_COUNT:
            break

        title, link, source = article["title"], article["link"], article["source"] or "News"
        print(f"\n[{built + 1}/{TARGET_STORY_COUNT}] Trying: {title[:70]}")

        tag = detect_category(title)
        article_url = resolve_article_url(link)

        # Slide A needs a REAL photo, not a generated background - this
        # preview is specifically to check what real-photo cards look like.
        img_path = os.path.join(TMP_DIR, f"{slugify(title)}.png")
        got_image = get_article_image_from_resolved_url(article_url, img_path)
        if not got_image:
            print("  -> no real photo available, skipping this story for the preview")
            skipped.append((title, "no image"))
            continue

        # Slide B needs real detail text. We extract the real paragraphs first
        # (grounding), then ask Gemini to turn them into a short hook line
        # (for slide A's headline) and a crisp detail summary (for slide B) -
        # falling back to the raw scraped versions if AI generation fails.
        paragraphs = extract_article_paragraphs(article_url)
        if len(paragraphs) < 2:
            print("  -> no usable article text found, skipping this story for the preview")
            skipped.append((title, "no text"))
            continue
        raw_article_text = " ".join(paragraphs)

        ai_hook, ai_detail = generate_hook_and_detail(title, raw_article_text, source)
        display_headline = ai_hook or title
        if ai_detail:
            slide_texts = [ai_detail]
            print(f"  -> AI hook: {display_headline!r}")
        else:
            print("  -> AI generation failed, falling back to raw scraped text")
            slide_texts = get_carousel_slide_texts(article_url, num_slides=1)
            if not slide_texts:
                skipped.append((title, "no text"))
                continue

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_filename = f"story{built + 1}_{timestamp}"

        paths = build_carousel(
            photo_path=img_path,
            headline=display_headline,
            source=source,
            tag=tag,
            slide_texts=slide_texts,
            out_dir=PREVIEW_DIR,
            base_filename=base_filename,
            grayscale=True,
            theme=theme,
        )
        print(f"  -> built {len(paths)} slides: {[os.path.basename(p) for p in paths]}")
        built += 1

    print(f"\nDone. Built {built}/{TARGET_STORY_COUNT} stories "
          f"({built * 2} images) in {PREVIEW_DIR}/.")
    if skipped:
        print(f"Skipped {len(skipped)} candidate(s):")
        for title, reason in skipped:
            print(f"  - [{reason}] {title[:70]}")
    print("\nNothing was posted, uploaded, or marked as posted - this is preview-only output.")


if __name__ == "__main__":
    run()
