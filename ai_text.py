"""
ai_text.py
Sends the resolved article's real extracted text to Gemini and asks for
two AI-written pieces of copy:
  - hook_text: short, punchy, eye-catching line for the slide-1 hook card
  - detail_text: crisp, informative summary for the slide-2 detail card

This replaces using the raw scraped headline/paragraphs verbatim with an
AI rewrite - still grounded in the real article content (we send Gemini
the actual scraped text, not just the URL), so it shouldn't invent facts,
but it IS generated text rather than a direct quote, so treat it as a
paraphrase/summary rather than verbatim reporting.

Requires at least one Gemini API key in .env - get one at
https://aistudio.google.com/apikey

Supports multiple keys so one key's exhausted daily free-tier quota
doesn't take down text generation / Hindi translation for the rest of
a run - once a key's quota is confirmed exhausted, it automatically
rotates to the next one. Set either:
  GEMINI_API_KEYS=key1,key2,key3       (comma-separated, preferred)
or:
  GEMINI_API_KEY_1=key1
  GEMINI_API_KEY_2=key2
  GEMINI_API_KEY_3=key3
(GEMINI_API_KEY alone still works too, as a single key.)
"""
import os
import json
import re
import time
import base64
import requests
from dotenv import load_dotenv

load_dotenv()


def _load_gemini_keys() -> list[str]:
    """
    Supports multiple Gemini API keys so a single key's exhausted daily
    quota doesn't take down text generation / translation for the rest
    of a run.

    Preferred: GEMINI_API_KEYS="key1,key2,key3" (comma-separated).
    Also accepted, for convenience/back-compat: GEMINI_API_KEY_1,
    GEMINI_API_KEY_2, ... GEMINI_API_KEY_N as separate env vars, and
    the original single GEMINI_API_KEY (used alone, or as an extra key
    on top of the above if both are set). Duplicates are dropped while
    preserving order.
    """
    keys: list[str] = []

    multi = os.environ.get("GEMINI_API_KEYS", "")
    for k in multi.split(","):
        k = k.strip()
        if k:
            keys.append(k)

    i = 1
    while True:
        k = os.environ.get(f"GEMINI_API_KEY_{i}")
        if not k:
            break
        k = k.strip()
        if k:
            keys.append(k)
        i += 1

    single = os.environ.get("GEMINI_API_KEY", "").strip()
    if single:
        keys.append(single)

    # de-dupe, preserve order
    seen = set()
    deduped = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            deduped.append(k)
    return deduped


GEMINI_API_KEYS = _load_gemini_keys()
# Kept for anything that only wants to check "is *a* key configured at all".
GEMINI_API_KEY = GEMINI_API_KEYS[0] if GEMINI_API_KEYS else None

# Index into GEMINI_API_KEYS of the key currently in use. Advances
# whenever the current key's daily quota gets confirmed exhausted (see
# _mark_current_key_exhausted), so later calls in the same run
# automatically pick up the next fresh key instead of failing.
_current_key_idx = 0
# Keys (by index into GEMINI_API_KEYS) already confirmed exhausted this
# run - skipped when rotating so we don't cycle back to a dead key.
_exhausted_key_indices: set[int] = set()
# Switched from gemini-3.6-flash: real-world usage (both this project's
# own AI Studio rate-limit dashboard and a separate working app on the
# same account) shows gemini-3.1-flash-lite getting a MUCH higher free-
# tier daily quota (500 RPD observed) than gemini-3.6-flash was getting
# (as low as ~20 RPD) - a ~25x difference. Flash-Lite is explicitly
# positioned by Google as the cost/throughput model for exactly this
# kind of workload (extraction, classification, short-form writing) as
# opposed to long reasoning chains, which fits hooks/captions/hashtags
# well. If you ever need to roll back, the old value was "gemini-3.6-flash".
GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# The free Flash tier is only ~10-15 requests/minute (in practice, daily
# quota is usually the binding constraint - gemini-3.1-flash-lite's free
# tier gives much more headroom here than gemini-3.6-flash did). This is
# a floor on the gap between any two Gemini calls, enforced process-wide,
# so a run never outruns the per-minute limit.
MIN_SECONDS_BETWEEN_CALLS = 6.0  # ~10 calls/min, safely under the tightest published limit
_last_call_at = 0.0

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = (15, 30, 60)  # used when Google doesn't send a Retry-After header

# Once a single call exhausts all its retries due to persistent 429s on
# the CURRENT key, that key's daily quota is almost certainly exhausted
# - a retry loop this thorough (105s of backoff) failing every time
# usually means the DAILY quota is exhausted, not a momentary per-minute
# burst (a burst would have cleared during that backoff). Rather than
# giving up outright, we rotate to the next configured Gemini key (see
# GEMINI_API_KEYS / _mark_current_key_exhausted) and retry fresh on
# that one. Only once EVERY configured key has been confirmed exhausted
# this run do we trip the breaker below and fall back to templated/raw
# text for good. Resets automatically on the next run (new process, new
# import).
_quota_exhausted = False


def _mark_current_key_exhausted():
    """
    Called when the key at GEMINI_API_KEYS[_current_key_idx] has just
    burned through MAX_RETRIES worth of 429s. Advances to the next
    not-yet-exhausted key if one exists; otherwise trips the global
    `_quota_exhausted` breaker so every subsequent call fails fast
    instead of repeating a 100s+ losing retry loop per remaining story.
    """
    global _current_key_idx, _quota_exhausted
    _exhausted_key_indices.add(_current_key_idx)

    for idx in range(len(GEMINI_API_KEYS)):
        if idx not in _exhausted_key_indices:
            print(f"[ai_text] Gemini key #{_current_key_idx + 1} exhausted - "
                  f"rotating to key #{idx + 1} of {len(GEMINI_API_KEYS)}.")
            _current_key_idx = idx
            return

    print(f"[ai_text] All {len(GEMINI_API_KEYS)} configured Gemini key(s) are exhausted - "
          f"falling back to templated/raw text for the rest of this run.")
    _quota_exhausted = True

PROMPT_TEMPLATE = """You are writing copy for a 2-slide Instagram news card, based on a real news article.

Article headline: {headline}
Source: {source}
Article text (real, extracted from the published article):
\"\"\"
{article_text}
\"\"\"

Write two pieces of copy, grounded ONLY in the article text above - do not add facts, numbers, or claims that aren't in it.

1. "hook": A short, punchy, attention-grabbing line for the first slide of the card. Maximum 12 words. This is the eye-catching headline that stops someone scrolling - clear and specific, not vague clickbait ("You won't believe..." is NOT allowed). It should tell the reader what actually happened.

2. "detail": A crisp, informative summary of the story for the second slide, 2-3 sentences, roughly 40-60 words. This is the "here's what's actually going on" explainer - factual, clear, no fluff, no clickbait, written like a good news brief.

3. "highlight": The single most scroll-stopping word or short phrase (1-3 words) taken VERBATIM from the "hook" text you just wrote - it must be an exact substring of "hook", same spelling and capitalization. Pick whatever carries the most punch: a number, a proper noun, a dramatic verb, or the sharpest phrase. This word/phrase will get a highlighter-marker box behind it on the card, so it should be the thing a reader's eye should land on first, not a filler word like "the" or "a".

4. "caption_paragraph": A detailed, well-written paragraph for this story's Instagram CAPTION (not the card image) - 4-7 sentences, roughly 90-150 words. Explain what actually happened: the key facts, numbers, names, and any important context or background from the article. Written like a real news outlet's Instagram caption - informative and thorough, no fluff, no clickbait, no invented facts. This is separate from "detail" above (which must stay short for the card image) - this one is longer and more explanatory since it's meant to fully inform someone who only reads the caption.
{sensitive_instruction}
Respond with ONLY a JSON object, no markdown fences, no preamble, in exactly this shape:
{{"hook": "...", "detail": "...", "highlight": "...", "caption_paragraph": "..."}}
"""

# Extra instruction spliced into the prompt above ONLY for stories flagged
# as sensitive/serious (deaths, sexual assault, murder, and similar) - see
# hourly_run.is_sensitive_story. Keeps the copy plain and factual instead
# of the usual punchy/attention-grabbing framing, which is inappropriate
# for this kind of story regardless of how well it otherwise "hooks".
SENSITIVE_INSTRUCTION = """
IMPORTANT - this story involves a death, sexual assault, murder, or similarly serious/tragic subject. Override the "punchy/attention-grabbing" instruction above: write BOTH the hook and the detail in a plain, factual, straightforward tone instead. No dramatic language, no sensational adjectives, no exclamation points, nothing that reads as "clickbait" even in spirit. State what happened directly and respectfully, the way a serious newspaper would, not the way a tabloid would.
"""


def _extract_json(text: str) -> dict | None:
    """Gemini sometimes wraps JSON in ```json fences despite instructions - strip them if present."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _wait_for_rate_limit_slot():
    """Enforce a minimum gap since the last Gemini call, process-wide."""
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    remaining = MIN_SECONDS_BETWEEN_CALLS - elapsed
    if remaining > 0:
        time.sleep(remaining)
    _last_call_at = time.monotonic()


def _call_gemini(prompt: str = None, timeout: int = 30, max_output_tokens: int = 4096,
                  response_schema: dict = None, parts: list = None) -> str | None:
    """Shared low-level call: sends a prompt (or a pre-built multimodal
    `parts` list - see is_real_news_photo) to Gemini, returns the raw
    text response (still possibly fenced JSON, unless response_schema
    is given) or None on any failure.

    parts: pass this INSTEAD of prompt for a multimodal request (e.g.
    an inline_data image part alongside a text part) - when given, it's
    sent as-is as the single user turn's `parts` array. When omitted,
    `prompt` is wrapped as the lone text part, same as before this
    parameter existed - every existing text-only caller is unaffected.

    max_output_tokens: raise this for prompts producing a lot of output
    (e.g. a batched multi-story call) - a tight cap truncates the JSON
    output mid-string, which shows up downstream as a parse failure
    rather than an obviously-related error.

    response_schema: an optional Gemini Schema dict (OBJECT/ARRAY/
    STRING/... types). When given, sets responseMimeType to
    application/json and responseSchema to this, so Gemini is
    constrained to emit valid JSON matching the shape directly - no
    markdown fences, no risk of missing fields, much more reliable than
    free-form "please respond with JSON" prompting alone for a large
    structured response (e.g. a whole batch of stories at once).

    Paces calls to stay under the free-tier RPM limit, and retries with
    backoff on 429 (rate limited) or transient network errors before
    giving up - honors Google's Retry-After header when it sends one.

    If quota was already confirmed exhausted earlier this run (see
    _quota_exhausted), returns None immediately without even trying -
    no point spending another 100+s to rediscover the same daily limit.
    Shared between text and vision calls: an image-classification call
    counts against the exact same daily quota as a text call, so if
    text generation already tripped the breaker this run, vision checks
    correctly skip too instead of burning more retries on a quota
    that's already known to be exhausted.
    """
    global _quota_exhausted

    if not GEMINI_API_KEYS:
        print("[ai_text] No Gemini API key set in .env (GEMINI_API_KEYS / GEMINI_API_KEY) - "
              "skipping AI text generation")
        return None

    if _quota_exhausted:
        return None

    request_parts = parts if parts is not None else [{"text": prompt}]

    generation_config = {
        "maxOutputTokens": max_output_tokens,
        "thinkingConfig": {"thinkingLevel": "low"},
    }
    if response_schema:
        generation_config["responseMimeType"] = "application/json"
        generation_config["responseSchema"] = response_schema

    # Outer loop: one pass per Gemini key we're willing to try for this
    # call (starts at whichever key is currently active - see
    # _mark_current_key_exhausted - and rotates forward on exhaustion).
    # Inner loop: the usual per-key retry/backoff on transient 429s.
    keys_tried = 0
    while keys_tried <= len(GEMINI_API_KEYS):
        if _quota_exhausted:
            return None
        keys_tried += 1
        current_key = GEMINI_API_KEYS[_current_key_idx]

        for attempt in range(MAX_RETRIES + 1):
            _wait_for_rate_limit_slot()
            try:
                resp = requests.post(
                    GEMINI_URL,
                    params={"key": current_key},
                    json={
                        "contents": [{"role": "user", "parts": request_parts}],
                        "generationConfig": generation_config,
                    },
                    timeout=timeout,
                )

                if resp.status_code == 429:
                    if attempt >= MAX_RETRIES:
                        print(f"[ai_text] Gemini key #{_current_key_idx + 1} still "
                              f"rate-limited after {MAX_RETRIES} retries. This usually means "
                              f"the daily quota on this key is exhausted (not a momentary "
                              f"burst, since that would have cleared during "
                              f"{sum(RETRY_BACKOFF_SECONDS)}s of backoff).")
                        _mark_current_key_exhausted()
                        break  # break inner retry loop, outer loop retries fresh on new key
                    wait_s = float(resp.headers.get("Retry-After", RETRY_BACKOFF_SECONDS[attempt]))
                    print(f"[ai_text] Gemini rate-limited (429) - waiting {wait_s:.0f}s before "
                          f"retry {attempt + 1}/{MAX_RETRIES}...")
                    time.sleep(wait_s)
                    continue

                resp.raise_for_status()
                data = resp.json()
                candidate = data["candidates"][0]
                finish_reason = candidate.get("finishReason")
                parts = candidate.get("content", {}).get("parts", [])
                if not parts:
                    print(f"[ai_text] Gemini returned no text (finishReason={finish_reason}) - "
                          f"likely ran out of token budget while thinking. Try raising maxOutputTokens.")
                    return None
                return parts[0]["text"]

            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt >= MAX_RETRIES:
                    print(f"[ai_text] Gemini request failed after {MAX_RETRIES} retries: {e}")
                    return None
                wait_s = RETRY_BACKOFF_SECONDS[attempt]
                print(f"[ai_text] Gemini request failed ({e}) - retrying in {wait_s}s "
                      f"({attempt + 1}/{MAX_RETRIES})...")
                time.sleep(wait_s)
                continue
            except (requests.RequestException, KeyError, IndexError) as e:
                print(f"[ai_text] Gemini request failed: {e}")
                return None

    return None


def _validate_highlight(highlight: str | None, hook: str | None) -> str | None:
    """
    The card renderer needs to find `highlight` as an exact substring of
    `hook` to know which pixels to draw the marker box behind. Gemini
    usually returns it verbatim, but occasionally paraphrases or changes
    case - in that case a highlight the renderer can't locate is worse
    than no highlight at all (silently drops the box), so we verify the
    match here (case-insensitively) and drop anything that doesn't line
    up rather than pass bad data downstream.
    """
    if not highlight or not hook:
        return None
    highlight = highlight.strip()
    if not highlight:
        return None
    if highlight.lower() not in hook.lower():
        print(f"[ai_text] highlight {highlight!r} not found verbatim in hook {hook!r} - dropping")
        return None
    return highlight


IMAGE_VERDICT_PROMPT = (
    "You are a quality filter for an Instagram news carousel. Look at this image, which "
    "was auto-scraped from a news article's page (its og:image/twitter:image tag or a "
    "same-page <img>). Decide whether it's a genuine, usable NEWS PHOTO - a real photograph "
    "depicting the event, people, place, or subject of a news story.\n\n"
    "REJECT (is_real_photo: false) if the image is:\n"
    "- A publication's own masthead/brand logo or watermark\n"
    "- A generic template graphic (e.g. a \"BREAKING NEWS\" banner, a solid-color card with "
    "text/headline overlaid instead of a photo, a generic \"LIVE UPDATES\" graphic)\n"
    "- A stock/decorative graphic unrelated to any specific real-world subject (icons, "
    "abstract art, a generic map/chart with no photographic content)\n"
    "- Mostly text, or a screenshot of a webpage/social post/tweet rather than a photo\n\n"
    "ACCEPT (is_real_photo: true) if it's an actual photograph of real people, places, "
    "objects, or events - a press photo, a photojournalism-style shot, a portrait, an "
    "official photo, etc. - even if it has minor text/logo overlays in a corner, as long as "
    "a real photograph is clearly the main content.\n\n"
    "Respond with ONLY a JSON object: "
    '{"is_real_photo": true or false, "reason": "one short phrase explaining why"}'
)

IMAGE_VERDICT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_real_photo": {"type": "BOOLEAN"},
        "reason": {"type": "STRING"},
    },
    "required": ["is_real_photo", "reason"],
}


def is_real_news_photo(image_path: str, timeout: int = 20) -> tuple[bool, str]:
    """
    Second-pass image filter, on top of image_fetch.is_usable_article_image's
    cheap pixel heuristics (resolution/aspect-ratio/dominant-color-
    coverage). Those catch most logos/banners for free with zero
    network calls, but can't tell a busy multi-color infographic or a
    collage-style banner from a real photo - only actual image
    understanding can. This asks Gemini directly.

    Meant to run ONLY on candidates that already passed the cheap
    heuristics (see image_fetch.download_image) - never as the sole or
    first-line filter, both to avoid spending Gemini quota on images
    that free checks would have rejected anyway, and so this feature
    degrades gracefully rather than being a single point of failure.

    Returns (is_real_photo, reason). FAILS OPEN on any problem - no API
    key, quota exhausted, network error, unparseable response - all
    return (True, "...") rather than (False, "..."), so a Gemini outage
    (or the same quota exhaustion that can hit the text-generation
    calls) degrades to "heuristics-only filtering", never to "silently
    reject every image". This mirrors run_combined's fallback-to-
    templated-text pattern for text generation - AI failure should
    degrade quality, not availability.
    """
    if not GEMINI_API_KEYS:
        return True, "No Gemini API key set - skipping vision check, heuristics-only"
    if _quota_exhausted:
        return True, "All Gemini keys already exhausted this run - skipping vision check"

    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
    except OSError as e:
        return True, f"couldn't read image file for vision check ({e}) - skipping"

    b64_data = base64.b64encode(image_bytes).decode("ascii")

    parts = [
        {"inline_data": {"mime_type": "image/png", "data": b64_data}},
        {"text": IMAGE_VERDICT_PROMPT},
    ]

    raw = _call_gemini(parts=parts, timeout=timeout, max_output_tokens=200,
                        response_schema=IMAGE_VERDICT_SCHEMA)
    if raw is None:
        return True, "Gemini vision call failed - skipping check, heuristics-only"

    verdict = _extract_json(raw)
    if verdict is None or "is_real_photo" not in verdict:
        return True, "Gemini returned an unparseable verdict - skipping check, heuristics-only"

    return bool(verdict["is_real_photo"]), verdict.get("reason", "")


def generate_hook_and_detail(headline: str, article_text: str, source: str, timeout: int = 30, sensitive: bool = False) -> tuple:
    """
    Calls Gemini with the real article text and asks for a hook line,
    detail summary, highlight phrase, and a longer caption paragraph.
    Returns (hook_text, detail_text, highlight_text, caption_paragraph)
    - any element may be None if generation failed or (for highlight)
    didn't validate, so callers should fall back to the raw scraped
    headline/paragraph text and/or skip the highlight box in that case.

    caption_paragraph is a separate, longer (4-7 sentence) write-up
    meant for the Instagram CAPTION rather than the card image - see
    PROMPT_TEMPLATE. Kept distinct from detail_text (which stays short
    so it still fits the card's second slide).

    sensitive: pass True for stories involving death, sexual assault,
    murder, or similarly serious/tragic subjects (see hourly_run.
    is_sensitive_story) - swaps the usual punchy/attention-grabbing
    instruction for a plain, factual tone instead.
    """
    if not article_text:
        return None, None, None, None

    prompt = PROMPT_TEMPLATE.format(
        headline=headline,
        source=source,
        article_text=article_text[:6000],  # keep prompt reasonably sized
        sensitive_instruction=SENSITIVE_INSTRUCTION if sensitive else "",
    )

    raw_text = _call_gemini(prompt, timeout=timeout)
    if not raw_text:
        return None, None, None, None

    parsed = _extract_json(raw_text)
    if not parsed:
        print(f"[ai_text] could not parse Gemini response as JSON: {raw_text[:200]!r}")
        return None, None, None, None

    hook = parsed.get("hook", "").strip() or None
    detail = parsed.get("detail", "").strip() or None
    highlight = _validate_highlight(parsed.get("highlight", "").strip() or None, hook)
    caption_paragraph = parsed.get("caption_paragraph", "").strip() or None
    return hook, detail, highlight, caption_paragraph


CAPTION_PROMPT_TEMPLATE = """You are writing the Instagram caption for a real news carousel post on a news account.

Headline: {headline}
Source: {source}
Category: {tag}
Article text (real, extracted from the published article):
\"\"\"
{article_text}
\"\"\"

Write two things, grounded ONLY in the article text above - do not add facts, numbers, or claims that aren't in it:

1. "caption": An engaging Instagram caption, 2-4 sentences. Informative and readable, written like a real news account's caption - not clickbait, no invented facts, no emoji spam (a couple emoji is fine if it fits the tone).

2. "hashtags": A list of 8-12 relevant hashtags as plain strings starting with "#", no spaces inside a tag. Mix a few broad news/discovery tags (e.g. #IndiaNews, #Breaking) with several specific to this story's actual topic, people, or place.
{sensitive_instruction}
Respond with ONLY a JSON object, no markdown fences, no preamble, in exactly this shape:
{{"caption": "...", "hashtags": ["#...", "#..."]}}
"""


def generate_caption_and_hashtags(headline: str, article_text: str, source: str, tag: str = "NEWS", timeout: int = 30, sensitive: bool = False) -> dict | None:
    """
    Calls Gemini once to produce the Instagram POST text (not the card
    images): a caption and a list of hashtags. Grounded in the real
    article text.

    sensitive: pass True for stories involving death, sexual assault,
    murder, or similarly serious/tragic subjects - swaps in a plain,
    factual tone instruction instead of the usual "engaging" framing.

    Note: this does NOT generate a song suggestion - that's a separate,
    once-per-run call (see suggest_song below), since only one post
    actually goes out per run, so there's no reason to generate a
    distinct song per candidate story the way earlier versions did.

    Returns a dict {"caption": str, "hashtags": list[str]} or None if
    generation/parsing failed - callers should fall back to a simple
    templated caption in that case.
    """
    if not article_text:
        return None

    prompt = CAPTION_PROMPT_TEMPLATE.format(
        headline=headline,
        source=source,
        tag=tag,
        article_text=article_text[:6000],
        sensitive_instruction=SENSITIVE_INSTRUCTION if sensitive else "",
    )

    raw_text = _call_gemini(prompt, timeout=timeout)
    if not raw_text:
        return None

    parsed = _extract_json(raw_text)
    if not parsed:
        print(f"[ai_text] could not parse Gemini caption response as JSON: {raw_text[:200]!r}")
        return None

    caption = (parsed.get("caption") or "").strip()
    hashtags = [h.strip() for h in (parsed.get("hashtags") or []) if h.strip()]

    if not caption or not hashtags:
        print(f"[ai_text] Gemini caption response missing required fields: {parsed!r}")
        return None

    return {"caption": caption, "hashtags": hashtags}


SONG_PROMPT_TEMPLATE = """You are picking ONE song to mention as a suggested soundtrack for a news Instagram post.

Headline: {headline}

Suggest ONE real, well-known, actually-existing song (with real title and artist - do not invent one) whose mood/genre fits the emotional tone of this story (energetic for a win, somber for a tragedy, triumphant for an announcement, tense for a crisis, etc). This is a text-only mention in the caption - it will NOT be attached as actual post audio.

Respond with ONLY a JSON object, no markdown fences, no preamble, in exactly this shape:
{{"title": "...", "artist": "...", "reason": "..."}}
"""


def suggest_song(headline: str, timeout: int = 30) -> dict | None:
    """
    Calls Gemini once for a single mood-matched song suggestion, keyed
    off just the headline (no article text needed - mood/genre matching
    doesn't need the full body). Meant to be called ONCE per real post
    (once inside run()), not once per candidate story in a batch trial -
    only one post actually goes out per run, so generating a distinct
    song per candidate the way earlier versions did was pure waste of
    scarce daily quota. A batch trial should call this at most once,
    for the one story it wants to preview a song example on.

    Returns {"title": str, "artist": str, "reason": str} or None.
    """
    prompt = SONG_PROMPT_TEMPLATE.format(headline=headline)
    raw_text = _call_gemini(prompt, timeout=timeout)
    if not raw_text:
        return None

    parsed = _extract_json(raw_text)
    if not parsed:
        print(f"[ai_text] could not parse Gemini song response as JSON: {raw_text[:200]!r}")
        return None

    title = (parsed.get("title") or "").strip()
    artist = (parsed.get("artist") or "").strip()
    reason = (parsed.get("reason") or "").strip()
    if not title or not artist:
        return None
    return {"title": title, "artist": artist, "reason": reason}


DIGEST_PROMPT_TEMPLATE = """You are writing the Instagram caption for a single round-up carousel post on a news account. The carousel contains {count} DIFFERENT top news stories, each contributing its own slide(s). They are listed below in priority order, but the caption itself should never say or imply that they are "ranked" or ordered by importance - just present them as today's top stories.

Stories:
{story_list}

Write two things, grounded ONLY in the headlines/summaries above - do not add facts, numbers, or claims that aren't in them:

1. "caption": A short intro line (1-2 sentences, e.g. "Today's top {count} stories" or similar in your own words), then a blank line, then a numbered list with ONE story per line and an actual line break (\n) between every numbered item - never run items together in a single paragraph or separate them with "/". Format each item exactly like:
1. Tightened headline for story one
2. Tightened headline for story two
(and so on)
Each line should be a tightened version of the story (not the full headline verbatim). Do not add commentary per story, just the tightened headline. No clickbait. Any story below marked "[SENSITIVE]" involves a death, sexual assault, murder, or similarly serious/tragic subject - write ONLY that line in a plain, factual, straightforward tone (no dramatic language, no sensational adjectives), while the rest of the list can keep its normal punchy tone. Never print the literal word "[SENSITIVE]" in your output - it's a marker for you only. IMPORTANT: never mention how the list is ordered or that it's "ranked" / "ranked by importance" / "in order of importance" etc. - just present the numbered list with no explanation of its sequencing.

2. "hashtags": A list of 10-15 relevant hashtags as plain strings starting with "#", no spaces inside a tag. Mix a few broad news/discovery/round-up tags (e.g. #IndiaNews, #TopStories, #NewsRoundup) with several covering the actual topics/people/places across these stories.

Respond with ONLY a JSON object, no markdown fences, no preamble, in exactly this shape:
{{"caption": "...", "hashtags": ["#...", "#..."]}}
"""


def generate_digest_caption_and_hashtags(stories: list, timeout: int = 30) -> dict | None:
    """
    Like generate_caption_and_hashtags, but for a single combined post
    covering MULTIPLE distinct stories (a "top N stories" carousel)
    instead of one story's own caption. Called ONCE per combined post,
    not once per story - keeps this to one extra Gemini call no matter
    how many stories are bundled into the carousel.

    stories: list of dicts, each with "headline" and "source" (and
    optionally "detail" for a short one-line summary), in the priority
    rank order they should be listed in the caption.

    Returns {"caption": str, "hashtags": list[str]} or None if
    generation/parsing failed - caller should fall back to a simple
    templated digest caption in that case.
    """
    if not stories:
        return None

    story_list = "\n".join(
        f"{i + 1}. {'[SENSITIVE] ' if s.get('sensitive') else ''}{s['headline']} (Source: {s.get('source', 'News')})"
        + (f" - {s['detail']}" if s.get("detail") else "")
        for i, s in enumerate(stories)
    )
    prompt = DIGEST_PROMPT_TEMPLATE.format(count=len(stories), story_list=story_list)

    raw_text = _call_gemini(prompt, timeout=timeout, max_output_tokens=2048)
    if not raw_text:
        return None

    parsed = _extract_json(raw_text)
    if not parsed:
        print(f"[ai_text] could not parse Gemini digest-caption response as JSON: {raw_text[:200]!r}")
        return None

    caption = (parsed.get("caption") or "").strip()
    hashtags = [h.strip() for h in (parsed.get("hashtags") or []) if h.strip()]

    if not caption or not hashtags:
        print(f"[ai_text] Gemini digest-caption response missing required fields: {parsed!r}")
        return None

    return {"caption": caption, "hashtags": hashtags}


HASHTAGS_ONLY_PROMPT_TEMPLATE = """You are picking Instagram hashtags for a single round-up carousel post on a news account. The carousel contains {count} DIFFERENT top news stories, listed below.

Stories:
{story_list}

Return a list of 10-15 relevant hashtags as plain strings starting with "#", no spaces inside a tag. Mix a few broad news/discovery/round-up tags ({broad_examples}) with several SPECIFIC to the actual topics, people, places, or organizations named in the stories above - these specific tags should change from post to post based on what's actually in the stories, not be generic filler.
{lang_instruction}
Respond with ONLY a JSON object, no markdown fences, no preamble, in exactly this shape:
{{"hashtags": ["#...", "#..."]}}
"""


def generate_digest_hashtags(stories: list, timeout: int = 20, lang: str = "en") -> list[str] | None:
    """
    Lightweight companion to generate_digest_caption_and_hashtags: asks
    Gemini for JUST a hashtag list grounded in the actual stories in
    this combined post, without also regenerating the caption text
    (build_combined_caption / build_combined_caption_hindi already
    build the caption itself from each story's own AI copy - this only
    fills in the hashtag line so it tracks the real story mix instead
    of being a fixed, story-agnostic default list).

    stories: list of dicts, each with "headline" and "source" (and
    optionally "detail" for a short one-line summary, "sensitive" for
    the same [SENSITIVE] handling as generate_digest_caption_and_hashtags).

    lang: "en" (default) or "hi" - swaps in a Hindi-audience broad-tag
    example set and asks for a few Hindi-script discovery tags mixed
    in, same spirit as the old DEFAULT_HASHTAGS_HI list but grounded
    in the actual stories instead of fixed.

    Returns a list of hashtag strings, or None if generation/parsing
    failed - caller should fall back to a fixed default hashtag list
    in that case.
    """
    if not stories:
        return None

    story_list = "\n".join(
        f"{i + 1}. {'[SENSITIVE] ' if s.get('sensitive') else ''}{s['headline']} (Source: {s.get('source', 'News')})"
        + (f" - {s['detail']}" if s.get("detail") else "")
        for i, s in enumerate(stories)
    )
    if lang == "hi":
        broad_examples = "e.g. #IndiaNews, #HindiNews, #आजकीखबर, #Breaking"
        lang_instruction = "\nThese stories are for a Hindi-language audience - include a few Hindi-script hashtags (like #आजकीखबर) alongside the English ones.\n"
    else:
        broad_examples = "e.g. #IndiaNews, #TopStories, #NewsRoundup"
        lang_instruction = ""

    prompt = HASHTAGS_ONLY_PROMPT_TEMPLATE.format(
        count=len(stories), story_list=story_list,
        broad_examples=broad_examples, lang_instruction=lang_instruction,
    )

    raw_text = _call_gemini(prompt, timeout=timeout, max_output_tokens=512)
    if not raw_text:
        return None

    parsed = _extract_json(raw_text)
    if not parsed:
        print(f"[ai_text] could not parse Gemini digest-hashtags response as JSON: {raw_text[:200]!r}")
        return None

    hashtags = [h.strip() for h in (parsed.get("hashtags") or []) if h.strip()]
    if not hashtags:
        print(f"[ai_text] Gemini digest-hashtags response missing hashtags: {parsed!r}")
        return None

    return hashtags


def format_instagram_caption(result: dict, source: str) -> str:
    """
    Assembles the final caption text posted to Instagram: the AI caption,
    a source credit line, and the hashtags.
    """
    parts = [result["caption"], f"Source: {source}"]

    if result.get("hashtags"):
        parts.append(" ".join(result["hashtags"]))

    return "\n\n".join(parts)


HINDI_STORY_TRANSLATE_PROMPT_TEMPLATE = """You are localizing a real news carousel slide (already written in English) for an Indian Hindi-language sister news Instagram page - same story, same facts, just told the way a Hindi news outlet would actually phrase it in natural, everyday Hindi (Devanagari script). Not a stiff literal translation.

Keep all numbers, dates, and facts exactly as given. Transliterate proper nouns (people, places, organizations, teams) into Devanagari using common Hindi-news convention (e.g. "United States" -> "अमेरिका", "Modi" -> "मोदी"), EXCEPT widely-used acronyms/brand names Hindi outlets normally keep in Roman script (e.g. "IPL", "ISRO", "GDP", "AI").
{sensitive_instruction}
HEADLINE (English, max ~12 words): {headline}
DETAIL (English, 2-3 sentences): {detail}
CAPTION PARAGRAPH (English, 4-7 sentences - this is the longer Instagram caption write-up, translate it in full, keeping every fact/number/name): {caption_paragraph}
{highlight_instruction}
Respond with ONLY a JSON object, no markdown fences, no preamble, in exactly this shape:
{{"headline": "...", "detail": "...", "caption_paragraph": "..."{highlight_json_field}}}
"""

# Spliced in only when the English hook had a validated highlight phrase,
# so we ask Gemini to point out the equivalent Devanagari phrase in its
# OWN translated "headline" - the English highlight substring almost
# never survives translation intact (different script, different word
# order), so we can't just reuse it; we need a fresh verbatim match
# against whatever Hindi text Gemini actually writes.
HINDI_HIGHLIGHT_INSTRUCTION_TEMPLATE = """
The English version highlights this phrase for emphasis: "{highlight_en}". In your Hindi HEADLINE above, mark the equivalent word or short phrase (1-3 words) that should get the same highlighter-box emphasis - it must be an EXACT substring of the Hindi "headline" you write, same script and spelling.
"""


def translate_story_to_hindi(headline: str, detail_text: str, timeout: int = 30, sensitive: bool = False,
                              highlight_en: str | None = None, caption_paragraph: str | None = None) -> tuple:
    """
    Translates an already-generated English hook headline + detail
    summary (+ optionally the longer caption paragraph) into natural
    Hindi news phrasing, for feeding straight into
    card_generator_hindi.build_carousel as the `headline` / a slide_texts
    entry for the Hindi-language carousel of the SAME story (same facts,
    same photo, same theme - just the text is localized).

    sensitive: pass True for stories involving death, sexual assault,
    murder, or similarly serious/tragic subjects - keeps the Hindi
    phrasing plain and factual, matching the English version's tone
    (see hourly_run.is_sensitive_story / SENSITIVE_INSTRUCTION).

    highlight_en: the validated English highlight substring (from
    generate_hook_and_detail), if any. When given, asks Gemini to also
    mark the equivalent Devanagari phrase in its translated headline -
    the English substring itself won't match post-translation, so this
    is a fresh pick grounded in the Hindi text actually written.

    caption_paragraph: the English caption_paragraph (from
    generate_hook_and_detail), if any - translated in full so the Hindi
    combined caption can carry the same amount of per-story detail as
    the English one. Pass None to skip (older callers / no paragraph
    generated for this story).

    Returns (headline_hi, detail_hi, highlight_hi, caption_paragraph_hi)
    - headline_hi/detail_hi may be None if translation/parsing failed,
    so callers should skip building the Hindi post for that story
    rather than posting an untranslated or partially-translated card.
    highlight_hi is None whenever highlight_en wasn't given, or if the
    returned phrase didn't validate as an exact substring of
    headline_hi. caption_paragraph_hi is None whenever caption_paragraph
    wasn't given, or translation/parsing failed.
    """
    if not headline:
        return None, None, None, None

    prompt = HINDI_STORY_TRANSLATE_PROMPT_TEMPLATE.format(
        headline=headline,
        detail=detail_text or "",
        caption_paragraph=caption_paragraph or "",
        sensitive_instruction=SENSITIVE_INSTRUCTION if sensitive else "",
        highlight_instruction=HINDI_HIGHLIGHT_INSTRUCTION_TEMPLATE.format(highlight_en=highlight_en) if highlight_en else "",
        highlight_json_field=', "highlight": "..."' if highlight_en else "",
    )
    raw_text = _call_gemini(prompt, timeout=timeout)
    if not raw_text:
        return None, None, None, None

    parsed = _extract_json(raw_text)
    if not parsed:
        print(f"[ai_text] could not parse Hindi story translation as JSON: {raw_text[:200]!r}")
        return None, None, None, None

    headline_hi = (parsed.get("headline") or "").strip() or None
    detail_hi = (parsed.get("detail") or "").strip() or None
    highlight_hi = _validate_highlight((parsed.get("highlight") or "").strip() or None, headline_hi) if highlight_en else None
    caption_paragraph_hi = (parsed.get("caption_paragraph") or "").strip() or None
    return headline_hi, detail_hi, highlight_hi, caption_paragraph_hi


SIMPLE_HINDI_TRANSLATE_PROMPT_TEMPLATE = """Translate the following English news text into natural, everyday Hindi (Devanagari script), the way an Indian Hindi news outlet would phrase it - not a stiff literal translation. Keep numbers, dates, and proper nouns accurate (transliterate people/places/organizations into Devanagari per common Hindi-news convention; keep widely-used acronyms like IPL/ISRO/GDP/AI in Roman script).

TEXT:
\"\"\"
{text}
\"\"\"

Respond with ONLY the translated Hindi text - no preamble, no quotes, no explanation, no markdown.
"""


def translate_text_to_hindi(text: str, timeout: int = 30) -> str | None:
    """
    Generic single-string Hindi translation - used for extra carousel
    body-text slides beyond the first (translate_story_to_hindi only
    covers headline + one detail slide in a single call; additional
    info slides, when present, go through this one at a time).
    """
    if not text:
        return None
    prompt = SIMPLE_HINDI_TRANSLATE_PROMPT_TEMPLATE.format(text=text)
    raw_text = _call_gemini(prompt, timeout=timeout)
    if not raw_text:
        return None
    cleaned = raw_text.strip().strip('"').strip()
    return cleaned or None


CAPTION_HINDI_TRANSLATE_PROMPT_TEMPLATE = """You are localizing a finished Instagram caption (for a combined multi-story news carousel) into Hindi, for a Hindi-language sister page of the same news account. Translate the prose portion into natural, everyday Hindi (Devanagari) the way a Hindi news outlet's Instagram caption would actually read - not a stiff literal translation. Keep story order and every fact, number, name, and source exactly as given in the English version.

Also produce a fresh hashtag list for the Hindi audience: a mix of Hindi-news discovery tags (e.g. #HindiNews, #IndiaNews, #आजकीखबर, #Breaking) and a few tags specific to this batch's actual topics. 8-12 hashtags.

ENGLISH CAPTION (source article credits and any existing hashtags are included below - ignore the old hashtags, you're writing new ones):
\"\"\"
{caption}
\"\"\"

Respond with ONLY a JSON object, no markdown fences, no preamble, in exactly this shape:
{{"caption": "...", "hashtags": ["#...", "#..."]}}
"""


def translate_caption_to_hindi(caption_en: str, timeout: int = 30) -> dict | None:
    """
    Translates a finished English Instagram caption into a Hindi caption
    + a fresh Hindi-relevant hashtag set. Returns {"caption": str,
    "hashtags": list[str]} or None on failure - callers should fall back
    to a simple templated Hindi caption in that case. Note:
    hourly_run.build_combined_caption_hindi no longer uses this
    function (it assembles Hindi captions from already-translated
    per-story text instead) - this is kept for build_single_caption_hindi
    (single-story posts) and any other single-caption callers.
    """
    if not caption_en:
        return None

    prompt = CAPTION_HINDI_TRANSLATE_PROMPT_TEMPLATE.format(caption=caption_en)
    raw_text = _call_gemini(prompt, timeout=timeout, max_output_tokens=2048)
    if not raw_text:
        return None

    parsed = _extract_json(raw_text)
    if not parsed:
        print(f"[ai_text] could not parse Hindi caption translation as JSON: {raw_text[:200]!r}")
        return None

    caption = (parsed.get("caption") or "").strip()
    hashtags = [h.strip() for h in (parsed.get("hashtags") or []) if h.strip()]

    if not caption:
        return None
    return {"caption": caption, "hashtags": hashtags}


if __name__ == "__main__":
    sample_headline = "Government announces new policy on renewable energy investment for 2027"
    sample_text = (
        "The policy sets a target of 50 gigawatts of new solar and wind capacity by 2027, "
        "backed by a dedicated infrastructure fund and streamlined land-acquisition rules for developers. "
        "Industry groups welcomed the announcement but flagged concerns about grid capacity and the pace "
        "of transmission-line approvals, which have historically lagged behind generation targets."
    )
    hook, detail, highlight = generate_hook_and_detail(sample_headline, sample_text, "Reuters")
    print("HOOK:", hook)
    print("DETAIL:", detail)
    print("HIGHLIGHT:", highlight)

    caption_result = generate_caption_and_hashtags(sample_headline, sample_text, "Reuters", tag="BUSINESS")
    if caption_result:
        print("\nCAPTION RESULT:", caption_result)
        print("\nFINAL CAPTION TEXT:\n", format_instagram_caption(caption_result, "Reuters"))
    else:
        print("\nCaption generation failed (check GEMINI_API_KEY).")
