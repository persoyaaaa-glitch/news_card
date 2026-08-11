"""
font_comparison.py
-------------------
Generates a side-by-side font comparison for the 4 fonts shipped in fonts/:
    - PlayfairDisplay-Bold.ttf
    - RuntimeRegular-m2Odx.otf
    - AvelineEleganzaRegular-KVqeA.otf
    - Norway-rvVR7.ttf

For EACH font, it builds the same 2 slides (hook + description) using the
SAME freshly-fetched photo, headline, tag, source and body text, and the
SAME headline theme (gradient/logo) — the only thing that changes between
runs is which font is used for every text role on the card (headline, tag,
pill, meta line, body copy). That keeps the comparison fair: you're judging
the font, not a different photo/story/color each time.

4 fonts x 2 slides = 8 images, written to output/font_comparison/.

Usage:
    python font_comparison.py
    python font_comparison.py --query "technology India"   # optional topic
"""
import argparse
import os
import sys

import card_generator as cg
import news_source
import image_fetch
import article_extract

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "font_comparison")
TMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_images")

FONTS = {
    "playfair": os.path.join(cg._FONT_DIR, "PlayfairDisplay-Bold.ttf"),
    "runtime": os.path.join(cg._FONT_DIR, "RuntimeRegular-m2Odx.otf"),
    "aveline": os.path.join(cg._FONT_DIR, "AvelineEleganzaRegular-KVqeA.otf"),
    "norway": os.path.join(cg._FONT_DIR, "Norway-rvVR7.ttf"),
}

# Fixed theme so every font run shares the exact same gradient/logo -
# only the typeface changes between the 4 runs.
FIXED_THEME = cg.HEADLINE_THEMES[0]


def fetch_story(query: str = None, max_attempts: int = 8):
    """
    Pull recent headlines and return the first one that yields BOTH a
    real article photo and usable body text. Returns:
        (photo_path, headline, source, tag, body_text)
    Falls back to a generated background / generic copy if nothing
    fetchable is found after max_attempts (so the script always produces
    output, even without a live connection).
    """
    items = news_source.fetch_news(query, limit=max_attempts) if query else news_source.fetch_top_headlines(limit=max_attempts)

    os.makedirs(TMP_DIR, exist_ok=True)

    for i, item in enumerate(items):
        link = item.get("link", "")
        if not link:
            continue

        article_url = image_fetch.resolve_article_url(link)
        photo_path = os.path.join(TMP_DIR, f"font_compare_source_{i}.png")
        got_image = image_fetch.get_article_image_from_resolved_url(article_url, photo_path)
        if not got_image:
            continue

        paragraphs = article_extract.extract_article_paragraphs(article_url)
        if len(paragraphs) < 2:
            continue
        slide_texts = article_extract.build_slide_texts(paragraphs, num_slides=1)
        if not slide_texts:
            continue

        headline = item["title"]
        source = item.get("source") or "News"
        tag = "NEWS"
        body_text = slide_texts[0]
        print(f"[font_comparison] using story: {headline!r} ({source})")
        return photo_path, headline, source, tag, body_text

    # Fallback: no live/fetchable story found - still produce a comparison
    # using a generated gradient background instead of a real photo.
    print("[font_comparison] WARNING: could not fetch a live article+image; "
          "falling back to a generated background so the comparison can still run.")
    headline = "Government announces new policy on renewable energy investment for 2027"
    source = "Reuters"
    tag = "BUSINESS"
    body_text = (
        "The policy sets a target of 50 gigawatts of new solar and wind capacity "
        "by 2027, backed by a dedicated infrastructure fund and streamlined "
        "land-acquisition rules for developers."
    )
    return None, headline, source, tag, body_text


def build_font_sample(font_name: str, font_path: str, photo_path, headline, source, tag, body_text):
    """Temporarily point every card_generator font role at font_path, render
    the hook + description slide, then restore the originals."""
    originals = (cg.FONT_HEADLINE, cg.FONT_TAG, cg.FONT_META, cg.FONT_BODY)
    cg.FONT_HEADLINE = cg.FONT_TAG = cg.FONT_META = cg.FONT_BODY = font_path

    try:
        hook_path = os.path.join(OUT_DIR, f"{font_name}_hook.png")
        cg.build_news_card(
            photo_path=photo_path,
            headline=headline,
            source=source,
            tag=tag,
            out_path=hook_path,
            slide_index=0,
            total_slides=2,
            theme=FIXED_THEME,
        )

        desc_path = os.path.join(OUT_DIR, f"{font_name}_description.png")
        cg.build_info_slide(
            photo_path=photo_path,
            body_text=body_text,
            tag=tag,
            slide_index=1,
            total_slides=2,
            out_path=desc_path,
            theme=FIXED_THEME,
        )
    finally:
        cg.FONT_HEADLINE, cg.FONT_TAG, cg.FONT_META, cg.FONT_BODY = originals

    return hook_path, desc_path


def main():
    parser = argparse.ArgumentParser(description="Render hook+description slides once per font, for comparison.")
    parser.add_argument("--query", default=None, help="Optional news search topic (default: top headlines)")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)

    photo_path, headline, source, tag, body_text = fetch_story(args.query)

    produced = []
    for font_name, font_path in FONTS.items():
        if not os.path.exists(font_path):
            print(f"[font_comparison] SKIPPING '{font_name}': file not found at {font_path}")
            continue
        hook_path, desc_path = build_font_sample(font_name, font_path, photo_path, headline, source, tag, body_text)
        produced.append(hook_path)
        produced.append(desc_path)
        print(f"[font_comparison] {font_name}: {hook_path}, {desc_path}")

    print(f"\nDone. {len(produced)} images written to {OUT_DIR}/")
    return produced


if __name__ == "__main__":
    main()
