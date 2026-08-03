"""
instagram_publish.py
Posts an image + caption directly to Instagram via the Graph API.
This is a two-step flow: create a media container pointing at a public
image URL, wait for Instagram to finish processing it, then publish it.
"""
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()  # harmless no-op if already loaded by the entry script (e.g. hourly_run.py)

GRAPH_API_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.instagram.com/{GRAPH_API_VERSION}"

IG_USER_ID = os.environ.get("IG_USER_ID")
def _access_token():
    """Read fresh each call - ensure_token_fresh() may have updated this env var after module import (e.g. after a Supabase-tracked token refresh)."""
    return os.environ.get("IG_ACCESS_TOKEN")


def _check_env():
    if not IG_USER_ID or not _access_token():
        raise RuntimeError("IG_USER_ID / IG_ACCESS_TOKEN not set in environment")


def _raise_with_detail(resp: requests.Response):
    """
    Like resp.raise_for_status(), but Meta's Graph API puts the actually
    useful info (error.message, error.code, error.error_subcode,
    error.error_user_msg, fbtrace_id - e.g. "publishing limit reached",
    "media not found", a content policy flag, etc.) in the JSON body,
    which raise_for_status() alone never surfaces. Without this, every
    failure just looks like a bare "400 Client Error" with no way to
    tell WHY it failed.
    """
    if resp.ok:
        return
    try:
        detail = resp.json()
    except ValueError:
        detail = resp.text
    raise requests.HTTPError(
        f"{resp.status_code} {resp.reason} for url {resp.url} - Graph API said: {detail}",
        response=resp,
    )


def create_media_container(image_url: str, caption: str) -> str:
    _check_env()
    resp = requests.post(
        f"{GRAPH_BASE}/{IG_USER_ID}/media",
        data={"image_url": image_url, "caption": caption, "access_token": _access_token()},
        timeout=30,
    )
    _raise_with_detail(resp)
    return resp.json()["id"]


def wait_for_container_ready(container_id: str, timeout: int = 90, poll_interval: int = 3) -> bool:
    """Instagram processes the image async - poll status_code until FINISHED."""
    _check_env()
    elapsed = 0
    while elapsed < timeout:
        resp = requests.get(
            f"{GRAPH_BASE}/{container_id}",
            params={"fields": "status_code", "access_token": _access_token()},
            timeout=15,
        )
        _raise_with_detail(resp)
        status = resp.json().get("status_code")
        if status == "FINISHED":
            return True
        if status == "ERROR":
            return False
        time.sleep(poll_interval)
        elapsed += poll_interval
    return False


def publish_container(container_id: str) -> str:
    _check_env()
    resp = requests.post(
        f"{GRAPH_BASE}/{IG_USER_ID}/media_publish",
        data={"creation_id": container_id, "access_token": _access_token()},
        timeout=30,
    )
    _raise_with_detail(resp)
    return resp.json()["id"]


def post_to_instagram(image_url: str, caption: str) -> str:
    """Full flow: create container -> wait until ready -> publish. Returns the published media ID."""
    container_id = create_media_container(image_url, caption)
    if not wait_for_container_ready(container_id):
        raise RuntimeError(f"Media container {container_id} failed to process in time")
    return publish_container(container_id)


def _is_transient_media_fetch_error(exc: requests.HTTPError) -> bool:
    """
    error_subcode 2207052 = "Media download failed. Media URI doesn't meet
    our requirements" - in practice this almost always means Instagram's
    fetcher hit the image URL before Supabase's CDN had finished
    propagating the just-uploaded file, not an actual malformed URL/type.
    It's worth a short retry rather than failing the whole post outright.
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return False
    try:
        return resp.json().get("error", {}).get("error_subcode") == 2207052
    except ValueError:
        return False


def create_carousel_item_container(image_url: str, max_retries: int = 3) -> str:
    """
    Create one child item of a carousel. Same idea as create_media_container,
    but marked is_carousel_item and - per the Graph API - must NOT include
    a caption (the caption belongs to the parent carousel container only).

    Retries with backoff on the "media download failed" error specifically
    (see _is_transient_media_fetch_error) since that's usually a CDN
    propagation race, not a real problem with the file - everything else
    still fails immediately.
    """
    _check_env()
    for attempt in range(1, max_retries + 1):
        resp = requests.post(
            f"{GRAPH_BASE}/{IG_USER_ID}/media",
            data={
                "image_url": image_url,
                "is_carousel_item": "true",
                "access_token": _access_token(),
            },
            timeout=30,
        )
        try:
            _raise_with_detail(resp)
            return resp.json()["id"]
        except requests.HTTPError as e:
            if attempt < max_retries and _is_transient_media_fetch_error(e):
                wait = 5 * attempt  # 5s, 10s, ...
                print(f"[instagram_publish] media fetch failed for {image_url} "
                      f"(likely CDN propagation lag) - retrying in {wait}s "
                      f"(attempt {attempt}/{max_retries})")
                time.sleep(wait)
                continue
            raise


def create_carousel_container(child_container_ids: list, caption: str) -> str:
    """Create the parent CAROUSEL container that references the already-created child items."""
    _check_env()
    resp = requests.post(
        f"{GRAPH_BASE}/{IG_USER_ID}/media",
        data={
            "media_type": "CAROUSEL",
            "children": ",".join(child_container_ids),
            "caption": caption,
            "access_token": _access_token(),
        },
        timeout=30,
    )
    _raise_with_detail(resp)
    return resp.json()["id"]


def post_carousel_to_instagram(image_urls: list, caption: str) -> str:
    """
    Full carousel flow: create + wait-for-ready on each child item image
    (2-10 images per Instagram's limit), then create the parent carousel
    container referencing all of them, then publish. Returns the
    published media ID.
    """
    if not (2 <= len(image_urls) <= 10):
        raise ValueError(f"Instagram carousels need 2-10 images, got {len(image_urls)}")

    child_ids = []
    for image_url in image_urls:
        child_id = create_carousel_item_container(image_url)
        if not wait_for_container_ready(child_id):
            raise RuntimeError(f"Carousel item container {child_id} failed to process in time")
        child_ids.append(child_id)

    carousel_id = create_carousel_container(child_ids, caption)
    if not wait_for_container_ready(carousel_id):
        raise RuntimeError(f"Carousel container {carousel_id} failed to process in time")
    return publish_container(carousel_id)


def find_recent_matching_post(caption: str, lookback_seconds: int = 600, caption_prefix_len: int = 60) -> str | None:
    """
    Meta's Graph API occasionally returns an error (e.g. the "action is
    blocked" / "Application request limit reached" spam-review response)
    for a media_publish call that actually succeeded on Instagram's side
    by the time the response came back. Treating that as an outright
    failure is dangerous: the caller will think nothing was posted and
    will surface the same stories again on the next run, which then
    posts near-duplicate content in quick succession - the exact pattern
    Meta's spam filters are watching for, making the block worse over
    time.

    Call this right after a publish call raises, BEFORE deciding the post
    truly failed. It checks the account's most recent media for one whose
    caption prefix matches (captions are AI-generated and effectively
    unique per post) and whose timestamp is within lookback_seconds of
    now. Returns that post's media ID if found, else None (meaning it's
    safe to conclude the publish really did fail).
    """
    try:
        resp = requests.get(
            f"{GRAPH_BASE}/{IG_USER_ID}/media",
            params={
                "fields": "id,caption,timestamp",
                "limit": 10,
                "access_token": _access_token(),
            },
            timeout=15,
        )
        _raise_with_detail(resp)
    except requests.RequestException as e:
        print(f"[instagram_publish] couldn't verify recent posts ({e}) - "
              f"treating the publish as failed")
        return None

    target_prefix = (caption or "")[:caption_prefix_len]
    now = time.time()

    for item in resp.json().get("data", []):
        item_caption = (item.get("caption") or "")[:caption_prefix_len]
        item_ts_str = item.get("timestamp")
        if not item_ts_str or item_caption != target_prefix:
            continue
        try:
            # Instagram timestamps look like "2026-08-03T18:07:41+0000"
            item_ts = time.mktime(time.strptime(item_ts_str[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        if abs(now - item_ts) <= lookback_seconds:
            return item["id"]

    return None


if __name__ == "__main__":
    print("Set IG_USER_ID and IG_ACCESS_TOKEN, then call post_to_instagram(image_url, caption)")
    print("or post_carousel_to_instagram([image_url, ...], caption) for a multi-slide post.")
