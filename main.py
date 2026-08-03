"""
main.py
Full pipeline: search news -> pick article -> download its image ->
generate a styled news card PNG ready for Instagram.

Usage:
    python main.py "technology India"
    python main.py                       # falls back to top headlines
"""
import sys
import os
from datetime import datetime

from news_source import fetch_news, fetch_top_headlines
from image_fetch import get_article_image
from card_generator import build_news_card

OUTPUT_DIR = "output"
TMP_IMAGE_DIR = "tmp_images"


def slugify(text: str, max_len: int = 40) -> str:
    keep = "".join(c if c.isalnum() or c == " " else "" for c in text)
    return "_".join(keep.lower().split())[:max_len]


def run(query: str = None, tag: str = "NEWS", max_attempts: int = 5):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(TMP_IMAGE_DIR, exist_ok=True)

    print(f"Fetching news for: {query or 'top headlines'}")
    articles = fetch_news(query, limit=max_attempts) if query else fetch_top_headlines(limit=max_attempts)

    if not articles:
        print("No articles found.")
        return None

    for article in articles:
        print(f"Trying: {article['title'][:70]}...")
        img_path = os.path.join(TMP_IMAGE_DIR, f"{slugify(article['title'])}.png")

        success = get_article_image(article["link"], img_path)
        if not success:
            print("  -> no usable image, trying next article")
            continue

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(OUTPUT_DIR, f"card_{timestamp}.png")

        build_news_card(
            photo_path=img_path,
            headline=article["title"],
            source=article["source"] or "News",
            tag=tag,
            out_path=out_path,
        )

        print(f"Done: {out_path}")
        return out_path

    print("No article in this batch had a usable image. Try a different query.")
    return None


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else None
    run(query)
