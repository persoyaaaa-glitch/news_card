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

# How far back to look for near-duplicate titles. Wide enough that an old
# story recycled by another outlet weeks later still gets caught, without
# scanning the entire history of the table on every single check.
_DEDUP_LOOKBACK_DAYS = 60

# Similarity ratio (difflib SequenceMatcher, 0-1) above which two titles
# are treated as "the same story" rather than coincidentally similar
# wording. 0.82 catches reworded/re-punctuated headlines of the same
# event while still telling apart two different stories on the same topic.
_TITLE_SIMILARITY_THRESHOLD = 0.82

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
    """
    Belt-and-suspenders duplicate check so a story is NEVER posted twice,
    even if a different outlet covers the same event under a different
    URL and a slightly reworded headline.

    recent_titles: optionally pass a pre-fetched list (from
    get_recent_titles()) to avoid a fresh DB round-trip per candidate
    when screening many articles in one run.
    """
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


if __name__ == "__main__":
    print("Set SUPABASE_URL and SUPABASE_SERVICE_KEY, then import functions from this module.")
