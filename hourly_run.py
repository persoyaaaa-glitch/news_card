"""
hourly_run.py
Runs once per hour via Railway's cron scheduler.

Flow:
  1. Wait a short random jitter delay so posts land at varying minutes
     past the hour rather than exactly on it - ordinary scheduling
     variety, not a fixed :00 every time.
  2. Pull India's current top news stories (ranked by Google News's own
     editorial ordering - our proxy for "biggest story right now").
  3. Skip anything already posted (checked against Supabase).
  4. Download the article's photo, resolve the real article URL, and
     extract real body text for the informational slides. A configurable
     fraction of posts use a generated abstract background instead of
     the source photo, purely for visual variety - those get an
     "ILLUSTRATIVE IMAGE" label since they're not the real story photo.
  5. Build a carousel: slide 1 is the eye-catching hook (photo + short
     headline), slides 2+ are real informational content extracted from
     the article, with a duotone-tinted version of the same photo. Falls
     back to a single-image post if no real body text could be extracted.
  6. Upload each slide to Supabase Storage to get public URLs
     (Instagram's API requires URLs, not raw files).
  7. Publish directly to Instagram via the Graph API (carousel or single
     image, depending on how many slides were built).
  8. Log it in Supabase so it's never reposted.
"""
import json
import os
import random
import time
from token_refresh import ensure_token_fresh
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()  # must run before importing supabase_client/instagram_publish, which read env vars at import time

from news_source import fetch_best_and_breaking_news
from image_fetch import get_article_image_from_resolved_url, resolve_article_url
from article_extract import get_carousel_slide_texts, extract_article_paragraphs
from card_generator import build_carousel, HEADLINE_THEMES
import card_generator_hindi
from supabase_client import is_duplicate_story, get_recent_titles, mark_as_posted, upload_carousel_images, get_state, save_state
from instagram_publish import post_carousel_to_instagram, post_to_instagram, find_recent_matching_post
from ai_text import (
    generate_hook_and_detail, generate_caption_and_hashtags,
    format_instagram_caption, generate_digest_caption_and_hashtags,
    translate_story_to_hindi, translate_text_to_hindi, translate_caption_to_hindi,
)

# Master switch for the Hindi sister page. Set POST_HINDI_PAGE=false in
# the environment to pause Hindi posting entirely (e.g. while its token
# is being re-set-up) without touching the English pipeline at all -
# every Hindi code path below checks this first and no-ops if it's off.
POST_HINDI = os.environ.get("POST_HINDI_PAGE", "true").strip().lower() == "true"

TMP_DIR = "tmp_images"
CARD_DIR = "output"

# Persists which HEADLINE_THEMES index goes out next, so consecutive
# combined posts visibly cycle silver -> bronze_gold -> warm_taupe ->
# silver -> ... in strict rotation instead of randomly (which could
# repeat the same theme back-to-back by chance). Stored next to this
# file - same pattern as daily_scheduler.py's scheduler_state.json -
# so the rotation survives a process restart instead of resetting.
def _next_theme() -> dict:
    """
    Returns the next theme in the rotation and advances/persists the
    pointer via Supabase (app_state key "theme_rotation") rather than a
    local JSON file - GitHub Actions runners start from a clean
    filesystem every run, so a local file would silently reset to index
    0 on every invocation instead of actually rotating.
    """
    state = get_state("theme_rotation", default={"next_index": 0})
    idx = state.get("next_index", 0) % len(HEADLINE_THEMES)
    theme = HEADLINE_THEMES[idx]

    try:
        save_state("theme_rotation", {"next_index": (idx + 1) % len(HEADLINE_THEMES)})
    except Exception as e:
        print(f"[hourly_run] warning: couldn't persist theme rotation state ({e}) - "
              f"rotation may repeat a theme on the next run")

    return theme


# Same rotation pattern as _next_theme() above, but for card_generator_hindi's
# two font families ("kalam", "eczar") - persisted via Supabase (app_state key
# "hindi_font_rotation") so consecutive Hindi posts strictly alternate
# fonts instead of repeating one by chance.
def _next_font_family() -> str:
    choices = card_generator_hindi.FONT_FAMILY_CHOICES
    state = get_state("hindi_font_rotation", default={"next_index": 0})
    idx = state.get("next_index", 0) % len(choices)
    family = choices[idx]

    try:
        save_state("hindi_font_rotation", {"next_index": (idx + 1) % len(choices)})
    except Exception as e:
        print(f"[hourly_run] warning: couldn't persist Hindi font rotation state ({e}) - "
              f"rotation may repeat a font on the next run")

    return family

# Fraction of posts that use a generated abstract background instead of
# the source article's photo. Set to 0 to always use the real photo -
# we only ever want to post stories whose real image could be fetched,
# never a story that falls back to a generated/illustrative background.
GENERATED_BG_RATIO = 0.0

# How many informational slides to try to build after the hook slide
# (actual count may be lower if the article doesn't yield enough real
# text - the pipeline falls back to a single-image post in that case).
NUM_INFO_SLIDES = 3

# Max random delay (seconds) added before each run, so posts land at
# varying minutes past the hour rather than exactly on the hour - this
# is just ordinary scheduling variety, not evasion of anything.
MAX_JITTER_SECONDS = 120  # up to 2 minutes

CATEGORY_KEYWORDS = {
    "POLITICS": ["election", "minister", "parliament", "government", "modi", "bjp", "congress party", "policy"],
    "BUSINESS": ["market", "stock", "economy", "rupee", "ipo", "startup", "rbi", "inflation", "sensex"],
    "SPORTS": ["cricket", "ipl", "football", "olympics", "match", "tournament", "player", "world cup"],
    "TECH": ["ai", "tech", "app", "smartphone", "software", "cyber", "data", "google", "meta"],
    "ENTERTAINMENT": ["bollywood", "film", "movie", "actor", "actress", "box office", "celebrity"],
    "WORLD": ["united states", "china", "pakistan", "united nations", "war", "president", "trump", "international"],
}

# Headline keywords/phrases that mark a story as a serious/sensitive
# subject - deaths, sexual violence, murder, and similar - so it gets
# special handling throughout the pipeline: a black-and-white card
# treatment (see card_generator.build_carousel's grayscale param),
# always-first placement in a combined carousel's slide order (see
# run_combined), and a plain/factual tone instead of the usual punchy
# copy (see ai_text.SENSITIVE_INSTRUCTION). Matched as whole words/
# phrases against the lowercased headline, same approach as
# detect_category above. Deliberately broad ("and similar ones" per
# the original ask) - false positives here just mean an extra story
# gets the more sober, respectful treatment, which is a safe default.
SENSITIVE_KEYWORDS = [
    "dies", "died", "dead", "death", "deaths", "killed", "kills",
    "murder", "murdered", "murders", "homicide", "manslaughter",
    "rape", "raped", "rapes", "gang rape", "gangrape",
    "sexual assault", "sexually assaulted", "molested", "molestation",
    "suicide", "suicides", "self-harm",
    "lynched", "lynching", "stabbed to death", "shot dead", "gunned down",
    "acid attack", "custodial death", "dowry death", "honor killing",
    "child abuse", "trafficking", "femicide",
]


def is_sensitive_story(headline: str) -> bool:
    """
    True if the headline involves a death, sexual assault, murder, or
    similarly serious/tragic subject - see SENSITIVE_KEYWORDS above.
    """
    lowered = headline.lower()
    return any(re.search(rf"\b{re.escape(kw)}\b", lowered) for kw in SENSITIVE_KEYWORDS)


def slugify(text: str, max_len: int = 40) -> str:
    keep = "".join(c if c.isalnum() or c == " " else "" for c in text)
    return "_".join(keep.lower().split())[:max_len]


def build_caption(headline: str, source: str, article_text: str = "", tag: str = "NEWS", breaking: bool = False, sensitive: bool = False) -> str:
    """
    AI-written caption + hashtags via Gemini, grounded in the real
    article text. Falls back to a simple templated caption if AI
    generation fails or there's no article text to ground it in.

    sensitive: pass True for stories involving death, sexual assault,
    murder, or similarly serious/tragic subjects (see
    is_sensitive_story below) - writes the caption in a plain,
    factual tone instead of the usual engaging framing.
    """
    prefix = "🚨 BREAKING\n\n" if breaking else ""
    if article_text:
        result = generate_caption_and_hashtags(headline, article_text, source, tag=tag, sensitive=sensitive)
        if result:
            caption = format_instagram_caption(result, source)
            return prefix + caption if breaking else caption
        print("  -> caption/hashtag generation failed, falling back to templated caption")
    fallback = f"{headline}\n\nSource: {source}\n\n#IndiaNews #News #Trending"
    if breaking:
        fallback += " #Breaking"
    return prefix + fallback


import re

def detect_category(headline: str) -> str:
    lowered = headline.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", lowered):
                return category
    return "NEWS"


def _build_post(article: dict, out_dir: str = CARD_DIR, tmp_dir: str = TMP_DIR,
                 base_filename: str = None, verbose: bool = True,
                 theme: dict = None, build_full_caption: bool = True) -> dict:
    """
    Does everything needed to turn one candidate article into a ready
    -to-post carousel + caption: resolves the real article URL, fetches
    a real photo (or falls back to a generated background), extracts
    real article text, gets AI hook/detail copy for the slides, builds
    the carousel images, and writes the final caption (with hashtags).
    Does NOT touch Instagram or Supabase.

    Shared by run() (single live/dry-run post) and run_batch() (the
    multi-story trial), so both always go through the exact same
    pipeline instead of two scripts quietly drifting apart.

    theme: pass the SAME HEADLINE_THEMES dict across every story being
    combined into one physical carousel (see run_combined) so the whole
    multi-story post shares one consistent gradient/logo look instead
    of each story's slides randomly picking their own. Leave None for a
    fresh random theme per story (the old, single-story-per-post
    behavior used by run() and run_multiple()).

    build_full_caption: whether to make the per-story caption+hashtags
    Gemini call at all. run_combined() sets this False, since a
    combined post uses ONE digest caption for the whole post (see
    generate_digest_caption_and_hashtags) rather than stitching 5
    separate full captions together - so there's no reason to spend a
    Gemini call generating individual captions that just get thrown
    away. When False, "caption" in the returned dict is "".

    Returns a dict with the built post plus diagnostic flags so callers
    can audit quality (used_real_image, has_description_slide).
    Also includes "detail_text" (the short AI-written detail summary,
    if one was generated) for callers that want a one-line blurb per
    story without the full formatted caption - e.g. run_combined()'s
    digest caption prompt.
    """
    title, link, source = article["title"], article["link"], article["source"] or "News"
    is_breaking = article.get("is_breaking", False)
    priority_rank = article.get("priority_rank")

    if verbose:
        rank_tag = f"[#{priority_rank}] " if priority_rank else ""
        print(f"Trying: {rank_tag}{'[BREAKING] ' if is_breaking else ''}{title[:60]}")
    tag = detect_category(title)
    sensitive = is_sensitive_story(title)
    use_generated_bg = random.random() < GENERATED_BG_RATIO

    article_url = resolve_article_url(link)

    img_path = None
    used_real_image = False
    if not use_generated_bg:
        img_path = os.path.join(tmp_dir, f"{slugify(title)}.png")
        if get_article_image_from_resolved_url(article_url, img_path):
            used_real_image = True
        else:
            # No real photo available - skip this story entirely rather
            # than silently falling back to a generated background. The
            # generated-background path is only for the deliberate
            # GENERATED_BG_RATIO variety above, not for fetch failures.
            if verbose:
                print("  -> no usable image, skipping this story")
            return None
    else:
        if verbose:
            print("  -> using generated background for visual variety (by design)")

    if verbose:
        print("  -> extracting article text...")
    paragraphs = extract_article_paragraphs(article_url)
    raw_article_text = " ".join(paragraphs)

    # AI-rewritten hook + detail text for the card images themselves -
    # falls back to the raw scraped headline/paragraphs if generation
    # fails or there isn't enough real article text to ground it in.
    display_headline = title
    detail_text = None
    slide_texts = []
    if len(paragraphs) >= 2:
        ai_hook, ai_detail = generate_hook_and_detail(title, raw_article_text, source, sensitive=sensitive)
        display_headline = ai_hook or title
        detail_text = ai_detail
        if ai_detail:
            slide_texts = [ai_detail]
        else:
            slide_texts = get_carousel_slide_texts(article_url, num_slides=NUM_INFO_SLIDES)
            detail_text = slide_texts[0] if slide_texts else None
    if not slide_texts and verbose:
        print("  -> no usable article text found, posting single-image (hook slide only)")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_filename = base_filename or f"card_{timestamp}"

    slide_paths = build_carousel(
        photo_path=img_path,  # None triggers the generated-background path
        headline=display_headline,
        source=source,
        tag=tag,
        slide_texts=slide_texts,
        out_dir=out_dir,
        base_filename=base_filename,
        breaking=is_breaking,
        theme=theme,
        grayscale=sensitive,
    )

    caption = ""
    if build_full_caption:
        caption = build_caption(
            display_headline, source, article_text=raw_article_text, tag=tag, breaking=is_breaking, sensitive=sensitive,
        )

    return {
        "title": title,
        "link": link,
        "source": source,
        "is_breaking": is_breaking,
        "is_sensitive": sensitive,
        "priority_rank": priority_rank,
        "tag": tag,
        "slide_paths": slide_paths,
        "caption": caption,
        "detail_text": detail_text,
        "slide_texts": slide_texts,  # raw list backing slide_paths[1:] - kept for the Hindi translation pass (_build_hindi_slides)
        # Diagnostics, for QA/trial runs to audit at a glance:
        "used_real_image": used_real_image,
        "used_generated_background": img_path is None,
        "had_article_text": bool(raw_article_text),
        "has_description_slide": len(slide_paths) > 1,
    }


def _build_hindi_slides(result: dict, theme: dict, out_dir: str = CARD_DIR, tmp_dir: str = TMP_DIR) -> dict | None:
    """
    Given an already-built English `result` dict (see _build_post),
    translates its headline + body text into Hindi and renders the SAME
    story as a Hindi-language carousel via card_generator_hindi, reusing
    the same photo, tag, theme, breaking flag, and grayscale/sensitive
    treatment as the English version - only the text differs.

    Returns a dict {"slide_paths": [...], "headline_hi": str,
    "detail_hi": str or None} or None if translation failed (e.g.
    Gemini quota exhausted) - callers should just skip the Hindi post
    for that story in that case, never post an untranslated card.
    """
    title = result["title"]
    tag = result["tag"]
    source = result["source"]
    sensitive = result.get("is_sensitive", False)
    # The first English slide is always the hook card (build_carousel's
    # convention); recover the same source photo path from tmp_dir using
    # the same slug _build_post used, so the Hindi hook/info slides crop
    # the identical photo instead of re-fetching or mismatching it.
    img_path = os.path.join(tmp_dir, f"{slugify(title)}.png")
    if not os.path.exists(img_path):
        img_path = None  # generated-background story - card_generator_hindi handles None fine

    slide_texts_en = result.get("slide_texts") or []
    first_body_en = slide_texts_en[0] if slide_texts_en else None

    # display_headline used for the English cards may differ from the raw
    # title (AI hook), but _build_post doesn't return it separately -
    # translate off of (title, first body chunk) as the grounding pair;
    # any extra body chunks beyond the first are translated individually
    # below with the plain single-string translator.
    headline_hi, detail_hi = translate_story_to_hindi(title, first_body_en, sensitive=sensitive)
    if not headline_hi:
        print(f"  -> [hi] translation failed for '{title[:50]}...' - skipping Hindi post for this story")
        return None

    slide_texts_hi = [detail_hi] if detail_hi else []
    # Extra info slides beyond the first (only happens on the raw-scrape
    # fallback path, when get_carousel_slide_texts yielded 2-3 chunks
    # instead of a single AI detail) - translate each one so the Hindi
    # carousel doesn't come up short of slides compared to the English one.
    for extra_chunk in slide_texts_en[1:]:
        translated_extra = translate_text_to_hindi(extra_chunk)
        if translated_extra:
            slide_texts_hi.append(translated_extra)
        else:
            print(f"  -> [hi] one extra info-slide translation failed for '{title[:50]}...' - "
                  f"Hindi carousel will have one fewer slide than English for this story")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_filename_hi = f"card_hi_{timestamp}_{slugify(title)}"

    slide_paths_hi = card_generator_hindi.build_carousel(
        photo_path=img_path,
        headline=headline_hi,
        source=source,
        tag=tag,
        slide_texts=slide_texts_hi,
        out_dir=out_dir,
        base_filename=base_filename_hi,
        breaking=result.get("is_breaking", False),
        theme=theme,
        grayscale=sensitive,
        font_family=_next_font_family(),
    )

    return {"slide_paths": slide_paths_hi, "headline_hi": headline_hi, "detail_hi": detail_hi}


def run(max_attempts: int = 30, apply_jitter: bool = True, dry_run: bool = False, include_global: bool = True):
    """
    Finds the single best not-yet-posted story and posts it.

    dry_run: if True, does everything up through building the carousel
    images and the final caption (with hashtags) but STOPS before
    uploading/publishing to Instagram and before marking the story as
    posted - safe to run repeatedly while testing. Returns a dict with
    the preview details instead of the Instagram media id.

    include_global: also fan out to international Google News + the
    global publisher feeds (GLOBAL_RSS_FEEDS), not just India coverage -
    see fetch_best_and_breaking_news for the full angle list. Defaults
    to True since the pipeline now ranks India and global stories on
    the same scale (see priority_rank) rather than treating them as
    separate pools.
    """
    ensure_token_fresh()
    if apply_jitter:
        jitter = random.randint(0, MAX_JITTER_SECONDS)
        print(f"[{datetime.now().isoformat()}] Waiting {jitter}s jitter before running...")
        time.sleep(jitter)

    os.makedirs(TMP_DIR, exist_ok=True)
    os.makedirs(CARD_DIR, exist_ok=True)

    print(f"[{datetime.now().isoformat()}] Surfacing best & breaking news (global={include_global})...")
    articles = fetch_best_and_breaking_news(country="IN", limit_per_query=max_attempts, include_global=include_global)

    if not articles:
        print("No articles returned. Exiting.")
        return

    # One DB round-trip for the fuzzy-dedup title list, reused across every
    # candidate in this batch instead of querying per-article.
    recent_titles = get_recent_titles()

    for article in articles:
        title, link, source = article["title"], article["link"], article["source"] or "News"

        if is_duplicate_story(title, link, recent_titles=recent_titles):
            print(f"Skipping (already posted / near-duplicate): {title[:60]}")
            continue

        result = _build_post(article)
        if result is None:
            continue  # no usable image for this story - try the next one
        slide_paths, caption = result["slide_paths"], result["caption"]

        if dry_run:
            print(f"  -> [DRY RUN] built {len(slide_paths)} slide(s), skipping upload/publish/mark-as-posted")
            return result

        print(f"  -> uploading {len(slide_paths)} slide(s) to Supabase Storage...")
        public_urls = upload_carousel_images(slide_paths)

        print("  -> posting to Instagram...")
        try:
            if len(public_urls) >= 2:
                media_id = post_carousel_to_instagram(public_urls, caption)
            else:
                media_id = post_to_instagram(public_urls[0], caption)
        except Exception as e:
            print(f"  -> Instagram publish failed: {e}")
            continue

        mark_as_posted(title, link, source, ig_media_id=media_id)
        print(f"Posted successfully ({len(slide_paths)} slide(s)). Media ID: {media_id}")
        result["media_id"] = media_id
        return result

    print("No article in this batch could be posted (all duplicates or missing images).")


# Random gap (seconds) between consecutive posts within a single
# run_multiple() call, so 10 posts don't all land in the same instant
# and read as a spam burst - separate from MAX_JITTER_SECONDS, which
# only delays the *start* of the whole run.
POST_GAP_SECONDS = (20, 75)


def run_multiple(story_count: int = 10, max_attempts: int = 80, apply_jitter: bool = True,
                  dry_run: bool = False, include_global: bool = True) -> list:
    """
    Posts up to `story_count` distinct stories in ONE invocation, walking
    the fetched candidates in priority order (priority_rank=1 - the
    single best story in the batch - goes out first, then #2, etc.).

    Each story becomes its OWN Instagram carousel post: hook slide +
    detail slide = 2 images per story. story_count=10 -> 20 images total,
    but as 10 separate posts, not one - Instagram carousels cap out at
    10 media items each, and 10 unrelated news stories crammed into a
    single carousel wouldn't make sense as one post anyway. A short
    random gap (POST_GAP_SECONDS) is inserted between consecutive posts
    so they don't all land in the same instant.

    This is the "many stories per run" entry point for the old
    one-post-per-story format (run_combined() is now the default -
    see bottom of this file - which bundles several stories into one
    combined carousel instead).

    dry_run: builds everything (images + caption) for up to story_count
    stories but never uploads, posts, or marks anything as posted -
    same safety net as run()'s dry_run, just for the whole batch.
    There's no inter-post gap in dry_run since nothing is actually
    hitting Instagram.

    Returns a list of result dicts (see _build_post for shape, plus
    "media_id" once posted), one per successfully posted/built story, in
    the order posted - which is also priority order.
    """
    ensure_token_fresh()
    if apply_jitter:
        jitter = random.randint(0, MAX_JITTER_SECONDS)
        print(f"[{datetime.now().isoformat()}] Waiting {jitter}s jitter before running...")
        time.sleep(jitter)

    os.makedirs(TMP_DIR, exist_ok=True)
    os.makedirs(CARD_DIR, exist_ok=True)

    print(f"[{datetime.now().isoformat()}] Surfacing candidates for {story_count} prioritized "
          f"posts (global={include_global})...")
    articles = fetch_best_and_breaking_news(country="IN", limit_per_query=max_attempts, include_global=include_global)

    if not articles:
        print("No articles returned. Exiting.")
        return []

    # One DB round-trip for the fuzzy-dedup title list, reused across
    # every candidate - and appended to in-memory as we post, so two
    # near-duplicate stories in the SAME run can't both go out even if
    # they weren't already caught by fetch_best_and_breaking_news's own
    # cross-angle dedup.
    recent_titles = get_recent_titles()
    posted = []

    for article in articles:
        if len(posted) >= story_count:
            break

        title, link, source = article["title"], article["link"], article["source"] or "News"

        if is_duplicate_story(title, link, recent_titles=recent_titles):
            print(f"Skipping (already posted / near-duplicate): {title[:60]}")
            continue

        result = _build_post(article)
        if result is None:
            continue  # no usable image for this story - try the next one
        slide_paths, caption = result["slide_paths"], result["caption"]

        if dry_run:
            print(f"  -> [DRY RUN] built {len(slide_paths)} slide(s) for priority #{result['priority_rank']}, "
                  f"skipping upload/publish/mark-as-posted")
            recent_titles.append(title)
            posted.append(result)
            continue

        print(f"  -> uploading {len(slide_paths)} slide(s) to Supabase Storage...")
        public_urls = upload_carousel_images(slide_paths)

        print(f"  -> posting priority #{result['priority_rank']} story to Instagram "
              f"({len(posted) + 1}/{story_count})...")
        try:
            if len(public_urls) >= 2:
                media_id = post_carousel_to_instagram(public_urls, caption)
            else:
                media_id = post_to_instagram(public_urls[0], caption)
        except Exception as e:
            print(f"  -> Instagram publish failed: {e}")
            continue

        mark_as_posted(title, link, source, ig_media_id=media_id)
        recent_titles.append(title)
        result["media_id"] = media_id
        posted.append(result)
        print(f"Posted {len(posted)}/{story_count} (priority #{result['priority_rank']}, "
              f"{len(slide_paths)} slide(s)). Media ID: {media_id}")

        if len(posted) < story_count:
            gap = random.randint(*POST_GAP_SECONDS)
            print(f"  -> waiting {gap}s before the next post...")
            time.sleep(gap)

    if len(posted) < story_count:
        print(f"\nOnly posted {len(posted)}/{story_count} stories this run "
              f"(ran out of eligible candidates - try raising max_attempts).")
    else:
        total_images = sum(len(r["slide_paths"]) for r in posted)
        print(f"\nDone: {len(posted)} stories, {total_images} images, posted in priority order.")

    return posted


def run_batch(story_count: int = 10, max_attempts: int = 60, out_dir: str = None, include_global: bool = True) -> list:
    """
    Builds `story_count` distinct, ready-to-post carousels + captions in
    one go, for a proper before-you-go-live QA trial - e.g. "20 images,
    10 stories" (10 stories x ~2 slides each). Runs the exact same
    pipeline as a real post (_build_post), but NEVER uploads to
    Supabase, NEVER posts to Instagram, and NEVER marks anything as
    posted - every story is still fully available for the real
    scheduled pipeline afterwards.

    Skips stories already posted before (via Supabase) and stories
    already picked earlier in this same batch, so you get `story_count`
    genuinely different stories rather than repeats.

    Two Gemini calls per story (hook+detail, then caption+hashtags) -
    comfortably inside gemini-3.1-flash-lite's free-tier daily quota.

    include_global: also fan out to international Google News + the
    global publisher feeds, not just India coverage - see
    fetch_best_and_breaking_news. Defaults to True.

    Returns a list of result dicts (see _build_post for shape), one per
    successfully-built story, in the order they were built.
    """
    out_dir = out_dir or CARD_DIR
    os.makedirs(TMP_DIR, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    print(f"[{datetime.now().isoformat()}] Surfacing candidates for a {story_count}-story trial (global={include_global})...")
    articles = fetch_best_and_breaking_news(country="IN", limit_per_query=max_attempts, include_global=include_global)
    if not articles:
        print("No articles returned. Exiting.")
        return []

    recent_titles = get_recent_titles()
    seen_this_batch = set()
    results = []

    for article in articles:
        if len(results) >= story_count:
            break

        title, link, source = article["title"], article["link"], article["source"] or "News"
        normalized = title.strip().lower()

        if normalized in seen_this_batch:
            continue
        if is_duplicate_story(title, link, recent_titles=recent_titles):
            print(f"Skipping (already posted / near-duplicate): {title[:60]}")
            continue
        seen_this_batch.add(normalized)

        print(f"\n[{len(results) + 1}/{story_count}]", end=" ")
        base_filename = f"trial_{len(results) + 1}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        result = _build_post(article, out_dir=out_dir, base_filename=base_filename)
        if result is None:
            continue  # no usable image for this story - try the next one
        rank_note = f" | priority=#{result['priority_rank']}" if result.get("priority_rank") else ""
        print(f"  -> built {len(result['slide_paths'])} slide(s) | "
              f"image={'real' if result['used_real_image'] else 'generated'} | "
              f"description_slide={'yes' if result['has_description_slide'] else 'no'}{rank_note}")
        results.append(result)

    if len(results) < story_count:
        print(f"\nOnly found {len(results)}/{story_count} usable, non-duplicate stories "
              f"(ran out of candidates - try raising max_attempts).")

    total_images = sum(len(r["slide_paths"]) for r in results)
    print(f"\nBuilt {len(results)} stories, {total_images} images total, in {out_dir}/. "
          "Nothing was posted, uploaded, or marked as posted.")
    return results


def build_combined_caption(results: list) -> str:
    """
    ONE caption for a combined multi-story carousel (see run_combined):
    an AI-written "top N stories" round-up naming every story in
    priority-rank order, plus a merged/deduped hashtag set (at least
    10 hashtags, guaranteed - see the top-up below). Falls back to a
    simple templated numbered list if Gemini generation fails.
    """
    stories = [
        {"headline": r["title"], "source": r["source"], "detail": r.get("detail_text"), "sensitive": r.get("is_sensitive", False)}
        for r in results
    ]
    digest = generate_digest_caption_and_hashtags(stories)

    if digest:
        hashtags = digest["hashtags"]
        if len(hashtags) < 10:
            fallback_tags = ["#IndiaNews", "#TopStories", "#NewsRoundup", "#Trending",
                              "#BreakingNews", "#DailyNews", "#WorldNews", "#NewsUpdate",
                              "#CurrentAffairs", "#NewsToday"]
            for tag in fallback_tags:
                if len(hashtags) >= 10:
                    break
                if tag not in hashtags:
                    hashtags.append(tag)
        parts = [digest["caption"], " ".join(hashtags)]
        return "\n\n".join(parts)

    # Templated fallback: simple numbered list, no AI needed.
    print("  -> digest caption generation failed, falling back to templated digest caption")
    lines = [f"Today's top {len(results)} stories:\n"]
    for i, r in enumerate(results, start=1):
        lines.append(f"{i}. {r['title']} (Source: {r['source']})")
    caption = "\n".join(lines)
    caption += (
        "\n\n#IndiaNews #TopStories #NewsRoundup #Trending #BreakingNews "
        "#DailyNews #WorldNews #NewsUpdate #CurrentAffairs #NewsToday"
    )
    return caption


def build_combined_caption_hindi(caption_en: str, results: list) -> str:
    """
    Hindi counterpart to build_combined_caption: translates the already-
    written English digest caption (same story order, same facts) into
    Hindi, with a fresh Hindi-relevant hashtag set. Falls back to a
    simple templated Hindi numbered list if translation fails, mirroring
    build_combined_caption's own English fallback.
    """
    translated = translate_caption_to_hindi(caption_en)
    if translated:
        hashtags = translated["hashtags"]
        if len(hashtags) < 10:
            fallback_tags = ["#IndiaNews", "#HindiNews", "#आजकीखबर", "#Trending",
                              "#BreakingNews", "#DailyNews", "#WorldNews", "#NewsUpdate",
                              "#CurrentAffairs", "#NewsToday"]
            for tag in fallback_tags:
                if len(hashtags) >= 10:
                    break
                if tag not in hashtags:
                    hashtags.append(tag)
        parts = [translated["caption"], " ".join(hashtags)]
        return "\n\n".join(parts)

    print("  -> [hi] digest caption translation failed, falling back to templated Hindi digest caption")
    lines = [f"आज की {len(results)} बड़ी खबरें:\n"]
    for i, r in enumerate(results, start=1):
        lines.append(f"{i}. {r['title']} (Source: {r['source']})")
    caption = "\n".join(lines)
    caption += (
        "\n\n#IndiaNews #HindiNews #आजकीखबर #Trending #BreakingNews "
        "#DailyNews #WorldNews #NewsUpdate #CurrentAffairs #NewsToday"
    )
    return caption


def run_combined(story_count: int = 5, images_per_story: int = 2, max_attempts: int = 80,
                  apply_jitter: bool = True, dry_run: bool = False, include_global: bool = True,
                  publish: bool = True) -> dict:
    """
    Posts `story_count` distinct stories bundled into ONE Instagram
    carousel post (default: 5 stories x 2 images each = 10 images,
    exactly Instagram's per-carousel cap), instead of run_multiple()'s
    behavior of one separate post per story. Walks candidates in
    priority order same as run_multiple, so slide 1-2 are the #1 story,
    slides 3-4 are #2, etc.

    How often this actually fires (once an hour, every 30 min, ...) is
    entirely up to the cron/scheduler calling this function - this
    function itself just builds and posts one combined batch per call.

    images_per_story caps each story's own slide count so the combined
    total stays predictable (story_count * images_per_story). If a
    story naturally yields fewer slides (e.g. no usable detail text,
    just the hook), that story simply contributes fewer images - the
    total can come in under the target, never over.

    Only ONE caption is generated for the whole post (see
    build_combined_caption) - not one per story.

    ALL story_count stories get marked as posted in Supabase (same
    ig_media_id, since they all went out as one post) so none of them
    get re-surfaced by a later run.

    Returns a dict: {"results": [...per-story dicts...], "media_id":
    str or None, "caption": str}. In dry_run, media_id is None and
    nothing is uploaded/posted/marked.

    publish: when False (and dry_run is False), images ARE uploaded to
    Supabase Storage and every story IS marked as posted (so it's never
    picked again), but the actual Instagram publish call is skipped and
    media_id comes back None. This is what content_pregen.py uses to
    pre-build a slot's content ahead of time for the companion app to
    preview/download, while still reserving those stories so a later
    slot doesn't pick the same ones. The real publish happens later,
    separately, when daily_scheduler.py fires that slot.

    Hindi sister page: when POST_HINDI is on (env POST_HINDI_PAGE, "true"
    by default), this ALSO builds a Hindi-translated carousel of the
    same story set via card_generator_hindi and posts it to the Hindi
    account, right after the English post. Returned dict gains
    "media_id_hi", "caption_hi", "image_urls_hi" alongside the existing
    English-only keys. A Hindi build/translate/publish failure never
    fails or blocks the English post - it's logged and the English
    result is still returned normally.
    """
    ensure_token_fresh(account="en")
    if POST_HINDI:
        ensure_token_fresh(account="hi")
    if apply_jitter:
        jitter = random.randint(0, MAX_JITTER_SECONDS)
        print(f"[{datetime.now().isoformat()}] Waiting {jitter}s jitter before running...")
        time.sleep(jitter)

    os.makedirs(TMP_DIR, exist_ok=True)
    os.makedirs(CARD_DIR, exist_ok=True)

    print(f"[{datetime.now().isoformat()}] Surfacing candidates for ONE combined "
          f"{story_count}-story / up to {story_count * images_per_story}-image post "
          f"(global={include_global})...")
    articles = fetch_best_and_breaking_news(country="IN", limit_per_query=max_attempts, include_global=include_global)

    if not articles:
        print("No articles returned. Exiting.")
        return {"results": [], "media_id": None, "caption": ""}

    recent_titles = get_recent_titles()
    results = []
    theme = _next_theme()  # rotates one-by-one across carousels; shared across every story in THIS post so it looks consistent

    for article in articles:
        if len(results) >= story_count:
            break

        title, link, source = article["title"], article["link"], article["source"] or "News"

        if is_duplicate_story(title, link, recent_titles=recent_titles):
            print(f"Skipping (already posted / near-duplicate): {title[:60]}")
            continue

        result = _build_post(article, theme=theme, build_full_caption=False)
        if result is None:
            continue  # no usable image for this story - try the next one

        # Cap this story's own slide count so the combined total stays
        # at/under story_count * images_per_story.
        result["slide_paths"] = result["slide_paths"][:images_per_story]

        recent_titles.append(title)
        results.append(result)
        print(f"  -> added priority #{result['priority_rank']} story "
              f"({len(result['slide_paths'])} slide(s)) to the combined post "
              f"({len(results)}/{story_count})")

    if not results:
        print("No usable candidates found this run.")
        return {"results": [], "media_id": None, "caption": ""}

    if len(results) < story_count:
        print(f"\nOnly found {len(results)}/{story_count} usable, non-duplicate stories - "
              f"posting a {len(results)}-story combined post instead (try raising max_attempts "
              f"for a fuller batch).")

    # Sensitive stories (deaths, sexual assault, murder, similar - see
    # is_sensitive_story) always lead the combined post, ahead of routine
    # stories regardless of their priority_score. This is a stable sort,
    # so relative order within the sensitive group and within the
    # non-sensitive group is otherwise preserved (still priority order).
    results.sort(key=lambda r: not r.get("is_sensitive", False))

    all_slide_paths = [p for r in results for p in r["slide_paths"]]
    if len(all_slide_paths) > 10:
        print(f"  -> {len(all_slide_paths)} images would exceed Instagram's 10-item carousel "
              f"cap, trimming lowest-priority story slides to fit")
        all_slide_paths = all_slide_paths[:10]

    caption = build_combined_caption(results)

    # --- Hindi sister-page content: same stories, translated text, same
    # photos/theme. Built regardless of dry_run/publish so a dry run
    # previews both languages - only the actual upload/publish calls
    # further down are gated on those flags, same as the English path.
    all_slide_paths_hi, caption_hi = [], ""
    if POST_HINDI:
        print(f"  -> [hi] translating and building the Hindi carousel for the same {len(results)} stories...")
        hi_results = []
        for r in results:
            hi = _build_hindi_slides(r, theme=theme)
            if hi is None:
                continue  # translation failed for this one story - it's simply absent from the Hindi post
            r["slide_paths_hi"] = hi["slide_paths"][:images_per_story]
            r["detail_hi"] = hi["detail_hi"]
            r["headline_hi"] = hi["headline_hi"]
            hi_results.append(r)
        all_slide_paths_hi = [p for r in hi_results for p in r["slide_paths_hi"]]
        if len(all_slide_paths_hi) > 10:
            all_slide_paths_hi = all_slide_paths_hi[:10]
        if all_slide_paths_hi:
            caption_hi = build_combined_caption_hindi(caption, results)
        else:
            print("  -> [hi] no stories translated successfully - skipping the Hindi post entirely this run")

    if dry_run:
        print(f"  -> [DRY RUN] built {len(results)} stories, {len(all_slide_paths)} images total "
              f"(+{len(all_slide_paths_hi)} Hindi images), skipping upload/publish/mark-as-posted")
        return {"results": results, "media_id": None, "caption": caption,
                "media_id_hi": None, "caption_hi": caption_hi}

    print(f"  -> uploading {len(all_slide_paths)} image(s) to Supabase Storage...")
    public_urls = upload_carousel_images(all_slide_paths)
    public_urls_hi = upload_carousel_images(all_slide_paths_hi) if all_slide_paths_hi else []

    if not publish:
        print(f"  -> publish=False (pre-generation mode): reserving these {len(results)} "
              f"stories now so no later slot today can pick them again, but NOT posting "
              f"to Instagram yet")
        for r in results:
            mark_as_posted(r["title"], r["link"], r["source"], ig_media_id=None)
            r["media_id"] = None
            r["image_urls"] = public_urls
        return {"results": results, "media_id": None, "caption": caption, "image_urls": public_urls,
                "media_id_hi": None, "caption_hi": caption_hi, "image_urls_hi": public_urls_hi}

    print(f"  -> posting ONE combined carousel ({len(results)} stories, {len(public_urls)} images) to Instagram (en)...")
    try:
        if len(public_urls) >= 2:
            media_id = post_carousel_to_instagram(public_urls, caption, account="en")
        else:
            media_id = post_to_instagram(public_urls[0], caption, account="en")
    except Exception as e:
        print(f"  -> Instagram publish failed (en): {e}")
        print(f"  -> checking the account's recent media in case it actually posted "
              f"despite the error (Meta sometimes returns an error for a call that "
              f"succeeded server-side)...")
        media_id = find_recent_matching_post(caption, account="en")
        if media_id:
            print(f"  -> confirmed: post {media_id} actually went live despite the error - "
                  f"marking these stories as posted so they aren't retried/duplicated")
            for r in results:
                mark_as_posted(r["title"], r["link"], r["source"], ig_media_id=media_id)
                r["media_id"] = media_id
            print(f"\nDone: post {media_id} confirmed live ({len(results)} stories, "
                  f"{len(public_urls)} images).")
        else:
            print(f"  -> confirmed: it genuinely did not post - leaving these stories "
                  f"unmarked so they can be retried")
            return {"results": results, "media_id": None, "caption": caption,
                     "media_id_hi": None, "caption_hi": caption_hi}
    else:
        for r in results:
            mark_as_posted(r["title"], r["link"], r["source"], ig_media_id=media_id)
            r["media_id"] = media_id
            r["image_urls"] = public_urls
        print(f"\nDone: posted 1 carousel with {len(results)} stories / {len(public_urls)} images "
              f"in priority order. Media ID: {media_id}")

    # --- Publish the Hindi post. English is never blocked or rolled back
    # by a Hindi failure (the English post above already happened) - a
    # Hindi publish problem is only ever logged here.
    media_id_hi = None
    if public_urls_hi:
        print(f"  -> posting the Hindi carousel ({len(public_urls_hi)} images) to Instagram (hi)...")
        try:
            if len(public_urls_hi) >= 2:
                media_id_hi = post_carousel_to_instagram(public_urls_hi, caption_hi, account="hi")
            else:
                media_id_hi = post_to_instagram(public_urls_hi[0], caption_hi, account="hi")
            print(f"  -> [hi] posted. Media ID: {media_id_hi}")
        except Exception as e:
            print(f"  -> [hi] Instagram publish failed: {e}")
            print("  -> [hi] checking the Hindi account's recent media in case it actually posted "
                  "despite the error...")
            media_id_hi = find_recent_matching_post(caption_hi, account="hi")
            if media_id_hi:
                print(f"  -> [hi] confirmed: post {media_id_hi} actually went live despite the error.")
            else:
                print("  -> [hi] confirmed: it genuinely did not post this run. The English post "
                      "already went out fine - this only affects the Hindi page.")

    return {"results": results, "media_id": media_id, "caption": caption, "image_urls": public_urls,
            "media_id_hi": media_id_hi, "caption_hi": caption_hi, "image_urls_hi": public_urls_hi}


def build_single_caption_hindi(caption_en: str) -> str:
    """
    Hindi counterpart to build_caption for a single-story post: translates
    the finished English caption (headline copy + hashtags) into Hindi
    with a fresh Hindi-relevant hashtag set, same pattern as
    build_combined_caption_hindi but for exactly one story. Falls back to
    the English caption text with a templated Hindi hashtag set appended
    if translation fails, mirroring the other fallbacks in this file.
    """
    translated = translate_caption_to_hindi(caption_en)
    if translated:
        hashtags = translated["hashtags"]
        if len(hashtags) < 10:
            fallback_tags = ["#IndiaNews", "#HindiNews", "#आजकीखबर", "#Trending",
                              "#BreakingNews", "#DailyNews", "#WorldNews", "#NewsUpdate",
                              "#CurrentAffairs", "#NewsToday"]
            for tag in fallback_tags:
                if len(hashtags) >= 10:
                    break
                if tag not in hashtags:
                    hashtags.append(tag)
        return "\n\n".join([translated["caption"], " ".join(hashtags)])

    print("  -> [hi] caption translation failed, falling back to the English caption + templated Hindi hashtags")
    return caption_en + (
        "\n\n#IndiaNews #HindiNews #आजकीखबर #Trending #BreakingNews "
        "#DailyNews #WorldNews #NewsUpdate #CurrentAffairs #NewsToday"
    )


def run_hindi_test(max_attempts: int = 30, dry_run: bool = True) -> dict | None:
    """
    Standalone test path for the Hindi sister page ONLY. Finds one real,
    not-yet-posted story exactly like run() does, but instead of posting
    it in English, translates it and posts (or previews) the Hindi
    carousel to the Hindi Instagram account.

    Deliberately never touches the English account (ensure_token_fresh is
    only called for account="hi" here, and post_carousel_to_instagram /
    post_to_instagram are only ever called with account="hi"), and
    deliberately never calls mark_as_posted - so a Hindi test run leaves
    the story completely untouched for the real automated pipeline. It
    can still be picked up and posted for real later (English or a real
    combined run) without being skipped as a duplicate.

    dry_run=True (default): builds everything - source photo/text,
    English draft caption (used only as translation input, never
    posted), the Hindi translation, and the Hindi carousel images - and
    returns/prints it, but does not upload or publish anything.

    dry_run=False: actually publishes the Hindi carousel to the Hindi
    Instagram account for real. Still never touches English and never
    marks anything as posted.
    """
    ensure_token_fresh(account="hi")

    os.makedirs(TMP_DIR, exist_ok=True)
    os.makedirs(CARD_DIR, exist_ok=True)

    print(f"[{datetime.now().isoformat()}] [hi-test] Surfacing candidates...")
    articles = fetch_best_and_breaking_news(country="IN", limit_per_query=max_attempts, include_global=True)
    if not articles:
        print("[hi-test] No articles returned. Exiting.")
        return None

    recent_titles = get_recent_titles()
    theme = random.choice(HEADLINE_THEMES)

    for article in articles:
        title, link, source = article["title"], article["link"], article["source"] or "News"

        if is_duplicate_story(title, link, recent_titles=recent_titles):
            print(f"[hi-test] Skipping (already posted / near-duplicate): {title[:60]}")
            continue

        result = _build_post(article, theme=theme)
        if result is None:
            continue  # no usable image for this story - try the next one

        print(f"  -> [hi-test] translating '{title[:60]}'...")
        hi = _build_hindi_slides(result, theme=theme)
        if hi is None:
            print("  -> [hi-test] translation failed for this story, trying the next candidate...")
            continue

        caption_hi = build_single_caption_hindi(result["caption"])
        slide_paths_hi = hi["slide_paths"]

        preview = {
            "title": title, "source": source, "link": link,
            "headline_hi": hi["headline_hi"], "slide_paths_hi": slide_paths_hi,
            "caption_hi": caption_hi,
        }

        if dry_run:
            print(f"  -> [hi-test][DRY RUN] built {len(slide_paths_hi)} Hindi slide(s), "
                  f"skipping upload/publish. English side was NOT touched or posted.")
            return preview

        print(f"  -> [hi-test] uploading {len(slide_paths_hi)} Hindi slide(s) to Supabase Storage...")
        public_urls_hi = upload_carousel_images(slide_paths_hi)

        print("  -> [hi-test] posting to the Hindi Instagram account only...")
        try:
            if len(public_urls_hi) >= 2:
                media_id_hi = post_carousel_to_instagram(public_urls_hi, caption_hi, account="hi")
            else:
                media_id_hi = post_to_instagram(public_urls_hi[0], caption_hi, account="hi")
        except Exception as e:
            print(f"  -> [hi-test] Instagram publish failed (hi): {e}")
            print("  -> [hi-test] checking the Hindi account's recent media in case it actually "
                  "posted despite the error...")
            media_id_hi = find_recent_matching_post(caption_hi, account="hi")
            if media_id_hi:
                print(f"  -> [hi-test] confirmed: post {media_id_hi} actually went live despite the error.")
            else:
                print("  -> [hi-test] confirmed: it genuinely did not post.")
                return None

        preview["media_id_hi"] = media_id_hi
        print(f"\n[hi-test] Done: posted to the Hindi account. Media ID: {media_id_hi}")
        print("[hi-test] Note: this story was NOT marked as posted, so it's still fully "
              "available for the real English/combined pipeline.")
        return preview

    print("[hi-test] No usable, non-duplicate candidate found in this batch. Try again, or raise max_attempts.")
    return None


if __name__ == "__main__":
    run_combined(story_count=5, images_per_story=2)
