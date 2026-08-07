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

Requires GEMINI_API_KEY in .env - get one at https://aistudio.google.com/apikey
"""
import os
import json
import re
import time
import requests
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
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

# Once a single call exhausts all its retries due to persistent 429s,
# further calls in the same run are almost certainly doomed too - a
# retry loop this thorough (105s of backoff) failing every time usually
# means the DAILY quota is exhausted, not a momentary per-minute burst
# (a burst would have cleared during that backoff). Retrying identically
# for every remaining story would just burn ~105s+ per call for nothing,
# so we trip a breaker: skip Gemini entirely (instant fallback to
# templated/raw text) for the rest of this process once this happens.
# Resets automatically on the next run (new process, new import).
_quota_exhausted = False

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
{sensitive_instruction}
Respond with ONLY a JSON object, no markdown fences, no preamble, in exactly this shape:
{{"hook": "...", "detail": "..."}}
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


def _call_gemini(prompt: str, timeout: int = 30, max_output_tokens: int = 4096,
                  response_schema: dict = None) -> str | None:
    """Shared low-level call: sends a prompt to Gemini, returns the raw
    text response (still possibly fenced JSON, unless response_schema
    is given) or None on any failure.

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
    """
    global _quota_exhausted

    if not GEMINI_API_KEY:
        print("[ai_text] GEMINI_API_KEY not set in .env - skipping AI text generation")
        return None

    if _quota_exhausted:
        return None

    generation_config = {
        "maxOutputTokens": max_output_tokens,
        "thinkingConfig": {"thinkingLevel": "low"},
    }
    if response_schema:
        generation_config["responseMimeType"] = "application/json"
        generation_config["responseSchema"] = response_schema

    for attempt in range(MAX_RETRIES + 1):
        _wait_for_rate_limit_slot()
        try:
            resp = requests.post(
                GEMINI_URL,
                params={"key": GEMINI_API_KEY},
                json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": generation_config,
                },
                timeout=timeout,
            )

            if resp.status_code == 429:
                if attempt >= MAX_RETRIES:
                    print(f"[ai_text] Gemini still rate-limited after {MAX_RETRIES} retries - "
                          f"giving up for this call. This usually means the daily quota is "
                          f"exhausted (not a momentary burst, since that would have cleared "
                          f"during {sum(RETRY_BACKOFF_SECONDS)}s of backoff), so skipping "
                          f"Gemini for the rest of this run - falling back to templated/raw "
                          f"text for every remaining story instead of repeating this same "
                          f"losing wait each time.")
                    _quota_exhausted = True
                    return None
                wait_s = float(resp.headers.get("Retry-After", RETRY_BACKOFF_SECONDS[attempt]))
                print(f"[ai_text] Gemini rate-limited (429) - waiting {wait_s:.0f}s before retry "
                      f"{attempt + 1}/{MAX_RETRIES}...")
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


def generate_hook_and_detail(headline: str, article_text: str, source: str, timeout: int = 30, sensitive: bool = False) -> tuple:
    """
    Calls Gemini with the real article text and asks for a hook line +
    detail summary. Returns (hook_text, detail_text) - either element
    may be None if generation failed, so callers should fall back to
    the raw scraped headline/paragraph text in that case.

    sensitive: pass True for stories involving death, sexual assault,
    murder, or similarly serious/tragic subjects (see hourly_run.
    is_sensitive_story) - swaps the usual punchy/attention-grabbing
    instruction for a plain, factual tone instead.
    """
    if not article_text:
        return None, None

    prompt = PROMPT_TEMPLATE.format(
        headline=headline,
        source=source,
        article_text=article_text[:6000],  # keep prompt reasonably sized
        sensitive_instruction=SENSITIVE_INSTRUCTION if sensitive else "",
    )

    raw_text = _call_gemini(prompt, timeout=timeout)
    if not raw_text:
        return None, None

    parsed = _extract_json(raw_text)
    if not parsed:
        print(f"[ai_text] could not parse Gemini response as JSON: {raw_text[:200]!r}")
        return None, None

    hook = parsed.get("hook", "").strip() or None
    detail = parsed.get("detail", "").strip() or None
    return hook, detail


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

Respond with ONLY a JSON object, no markdown fences, no preamble, in exactly this shape:
{{"headline": "...", "detail": "..."}}
"""


def translate_story_to_hindi(headline: str, detail_text: str, timeout: int = 30, sensitive: bool = False) -> tuple:
    """
    Translates an already-generated English hook headline + detail
    summary into natural Hindi news phrasing, for feeding straight into
    card_generator_hindi.build_carousel as the `headline` / a slide_texts
    entry for the Hindi-language carousel of the SAME story (same facts,
    same photo, same theme - just the text is localized).

    sensitive: pass True for stories involving death, sexual assault,
    murder, or similarly serious/tragic subjects - keeps the Hindi
    phrasing plain and factual, matching the English version's tone
    (see hourly_run.is_sensitive_story / SENSITIVE_INSTRUCTION).

    Returns (headline_hi, detail_hi) - either may be None if
    translation/parsing failed, so callers should skip building the
    Hindi post for that story rather than posting an untranslated or
    partially-translated card.
    """
    if not headline:
        return None, None

    prompt = HINDI_STORY_TRANSLATE_PROMPT_TEMPLATE.format(
        headline=headline,
        detail=detail_text or "",
        sensitive_instruction=SENSITIVE_INSTRUCTION if sensitive else "",
    )
    raw_text = _call_gemini(prompt, timeout=timeout)
    if not raw_text:
        return None, None

    parsed = _extract_json(raw_text)
    if not parsed:
        print(f"[ai_text] could not parse Hindi story translation as JSON: {raw_text[:200]!r}")
        return None, None

    headline_hi = (parsed.get("headline") or "").strip() or None
    detail_hi = (parsed.get("detail") or "").strip() or None
    return headline_hi, detail_hi


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
    to a simple templated Hindi caption in that case (see
    hourly_run.build_combined_caption_hindi).
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
    hook, detail = generate_hook_and_detail(sample_headline, sample_text, "Reuters")
    print("HOOK:", hook)
    print("DETAIL:", detail)

    caption_result = generate_caption_and_hashtags(sample_headline, sample_text, "Reuters", tag="BUSINESS")
    if caption_result:
        print("\nCAPTION RESULT:", caption_result)
        print("\nFINAL CAPTION TEXT:\n", format_instagram_caption(caption_result, "Reuters"))
    else:
        print("\nCaption generation failed (check GEMINI_API_KEY).")
