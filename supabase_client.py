"""
supabase_client.py

Three jobs:
1. Track which articles have already been posted, so no story is ever
   repeated - both an exact link check AND a fuzzy title check (catches
   the same story re-published under a different URL/source with a
   slightly reworded headline).
2. Host each generated card image in Supabase Storage so we have a
   public URL to hand Instagram's Graph API (it requires image_url,
   not a raw file upload).
3. Read/write the app_state key-value table and the schedule_overrides /
   slot_overrides tables the companion PWA writes to.
"""

import difflib
import os
from datetime import datetime, timedelta, timezone

from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()  # harmless no-op if already loaded by the entry script (e.g. hourly_run.py)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")  # service role key, NOT the anon/public key
STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "news-cards")

_DEDUP_LOOKBACK_DAYS = 60
_TITLE_SIMILARITY_THRESHOLD = 0.82

# Maps a language key to the slot_overrides column that holds its manual
# flag, added by migration_hi_manual_flag.sql (which renamed the
# original single `manual` column to `manual_en` and added `manual_hi`).
_MANUAL_COLUMN_BY_LANG = {"en": "manual_en", "hi": "manual_hi"}

_client: Client = None


def get_client() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_SERVICE_KEY not set in environment. "
                "Use the service_role key (Project Settings -> API), not the anon key."
            )
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


def is_already_posted(article_link: str) -> bool:
    client = get_client()
    resp = client.table("posted_articles").select("id").eq("link", article_link).limit(1).execute()
    return len(resp.data) > 0


def _normalize(text: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in text).split())


def get_recent_titles(days: int = _DEDUP_LOOKBACK_DAYS) -> list:
    """Titles posted within the lookback window, for fuzzy dedup."""
    client = get_client()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    resp = client.table("posted_articles").select("title").gte("posted_at", cutoff).execute()
    return [row["title"] for row in resp.data]


def is_duplicate_story(title: str, link: str, recent_titles: list = None) -> bool:
    if is_already_posted(link):
        return True
    normalized = _normalize(title)
    candidates = recent_titles if recent_titles is not None else get_recent_titles()
    for prior_title in candidates:
        if difflib.SequenceMatcher(None, normalized, _normalize(prior_title)).ratio() >= _TITLE_SIMILARITY_THRESHOLD:
            return True
    return False


def mark_as_posted(title: str, link: str, source: str, ig_media_id: str = None):
    client = get_client()
    client.table("posted_articles").insert({
        "title": title,
        "link": link,
        "source": source,
        "ig_media_id": ig_media_id,
    }).execute()


def upload_card_image(local_path: str, remote_filename: str) -> str:
    """Upload a generated card image (JPEG) to Supabase Storage and return its public URL."""
    client = get_client()
    with open(local_path, "rb") as f:
        file_bytes = f.read()
    ext = os.path.splitext(local_path)[1].lower()
    content_type = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
    client.storage.from_(STORAGE_BUCKET).upload(
        path=remote_filename,
        file=file_bytes,
        file_options={"content-type": content_type, "upsert": "true"},
    )
    return client.storage.from_(STORAGE_BUCKET).get_public_url(remote_filename)


def upload_carousel_images(local_paths: list) -> list:
    """Upload each slide of a carousel and return their public URLs, in the same order."""
    return [
        upload_card_image(path, os.path.basename(path))
        for path in local_paths
    ]


def get_state(key: str, default=None):
    """
    Read a value from the app_state key/value table. Used in place of
    local JSON state files (scheduler_state.json, theme_state.json,
    token_state.json) so state survives across GitHub Actions runs,
    where each run starts on a fresh filesystem.
    """
    client = get_client()
    resp = client.table("app_state").select("value").eq("key", key).limit(1).execute()
    if resp.data:
        return resp.data[0]["value"]
    return default


def save_state(key: str, value):
    """Upsert a value into the app_state key/value table."""
    client = get_client()
    client.table("app_state").upsert({
        "key": key,
        "value": value,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


def get_manual_slot_indices(slot_date: str, lang: str = "en") -> set:
    """
    Slot indices the PWA has flagged as 'I'm posting this one myself',
    for the given date AND language (see slot_overrides table /
    migration_hi_manual_flag.sql, which split the original single
    `manual` column into `manual_en` and `manual_hi`).

    Two things changed from the original single-language version:
    1. Added `lang` ("en" or "hi", defaults to "en" so any old caller
       that doesn't pass it keeps working) to read the right column.
    2. Now actually filters on that column's value (`.eq(column, True)`).
       The original version selected slot_index for every row that
       existed in slot_overrides for the date, full stop - it never
       checked whether `manual` was true or false, so a row explicitly
       set to false would still have been treated as manual. Filtering
       on the value is the correct behavior and is what daily_scheduler.py
       has always assumed this function does.

    Returns an empty set - i.e. "no manual slots" - if the query fails
    for any reason (table/column missing, RLS issue, transient network
    error) or if `lang` isn't recognized. This is called unconditionally
    on EVERY check_once() run, so it must never take the whole scheduler
    down; failing "no manual overrides" is always safe (worst case a
    slot that should've been manual gets auto-posted instead of skipped,
    which is far better than the entire day's posting silently stopping).
    """
    column = _MANUAL_COLUMN_BY_LANG.get(lang)
    if column is None:
        print(f"[supabase_client] get_manual_slot_indices got unknown lang={lang!r}, "
              f"treating as 'no manual slots'")
        return set()
    try:
        client = get_client()
        resp = (
            client.table("slot_overrides")
            .select("slot_index")
            .eq("slot_date", slot_date)
            .eq(column, True)
            .execute()
        )
        return {row["slot_index"] for row in resp.data}
    except Exception as e:
        print(f"[supabase_client] get_manual_slot_indices failed for lang={lang}, "
              f"treating as 'no manual slots': {e}")
        return set()


def get_schedule_override(slot_date: str):
    """
    Reads the PWA's requested schedule change for `slot_date` (see
    schedule_overrides table / supabase_app_additions.sql) - target post
    count for the day and/or specific times for individual not-yet-posted
    slots (now per language - see daily_scheduler.py's _apply_time_edits
    for the {"0": {"en": "...", "hi": "..."}} shape). Returns None if
    nothing's been requested, or if the table doesn't exist yet -
    daily_scheduler.py treats that exactly like "no override," so this
    is safe to call before any SQL migration.
    """
    try:
        client = get_client()
        resp = client.table("schedule_overrides").select("*").eq("slot_date", slot_date).limit(1).execute()
        return resp.data[0] if resp.data else None
    except Exception:
        return None


if __name__ == "__main__":
    print("Set SUPABASE_URL and SUPABASE_SERVICE_KEY, then import functions from this module.")
