"""
instagram_publish.py
Posts an image + caption directly to Instagram via the Graph API.
This is a two-step flow: create a media container pointing at a public
image URL, wait for Instagram to finish processing it, then publish it.

Multi-account: this file now drives TWO Instagram professional accounts
from one process - the English page ("en") and the Hindi page ("hi").
Every function takes an `account` kwarg (defaults to "en" so existing
callers that don't pass it keep working unchanged). Add a third
language/page later by adding one entry to ACCOUNTS below - nothing
else in this file needs to change.
"""
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()  # harmless no-op if already loaded by the entry script (e.g. hourly_run.py)

GRAPH_API_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.instagram.com/{GRAPH_API_VERSION}"

# Maps a short account key to the env var names holding that account's
# IG user id / access token. token_refresh.py writes refreshed tokens
# back into these same env vars at runtime (see ensure_token_fresh),
# so always read them fresh via os.environ.get() rather than caching.
ACCOUNTS = {
    "en": {"user_id_env": "IG_USER_ID", "token_env": "IG_ACCESS_TOKEN"},
    "hi": {"user_id_env": "IG_USER_ID_HI", "token_env": "IG_ACCESS_TOKEN_HI"},
}


def _user_id(account: str) -> str:
    return os.environ.get(ACCOUNTS[account]["user_id_env"])


def _access_token(account: str) -> str:
    """Read fresh each call - ensure_token_fresh() may have updated this
    env var after module import (e.g. after a Supabase-tracked token
    refresh for that specific account)."""
    return os.environ.get(ACCOUNTS[account]["token_env"])


def _check_env(account: str):
    if account not in ACCOUNTS:
        raise RuntimeError(
            f"Unknown IG account '{account}' - valid keys are {list(ACCOUNTS)}. "
            f"Add a new entry to ACCOUNTS in instagram_publish.py for a new page/language."
        )
    if not _user_id(account) or not _access_token(account):
        cfg = ACCOUNTS[account]
        raise RuntimeError(
            f"{cfg['user_id_env']} / {cfg['token_env']} not set in environment "
            f"(account='{account}')"
        )


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


def create_media_container(image_url: str, caption: str, account: str = "en") -> str:
    _check_env(account)
    resp = requests.post(
        f"{GRAPH_BASE}/{_user_id(account)}/media",
        data={"image_url": image_url, "caption": caption, "access_token": _access_token(account)},
        timeout=30,
    )
    _raise_with_detail(resp)
    return resp.json()["id"]


def wait_for_container_ready(container_id: str, account: str = "en", timeout: int = 90, poll_interval: int = 3) -> bool:
    """Instagram processes the image async - poll status_code until FINISHED."""
    _check_env(account)
    elapsed = 0
    while elapsed < timeout:
        resp = requests.get(
            f"{GRAPH_BASE}/{container_id}",
            params={"fields": "status_code", "access_token": _access_token(account)},
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


def publish_container(container_id: str, account: str = "en") -> str:
    _check_env(account)
    resp = requests.post(
        f"{GRAPH_BASE}/{_user_id(account)}/media_publish",
        data={"creation_id": container_id, "access_token": _access_token(account)},
        timeout=30,
    )
    _raise_with_detail(resp)
    return resp.json()["id"]


def post_to_instagram(image_url: str, caption: str, account: str = "en") -> str:
    """Full flow: create container -> wait until ready -> publish. Returns the published media ID."""
    container_id = create_media_container(image_url, caption, account=account)
    if not wait_for_container_ready(container_id, account=account):
        raise RuntimeError(f"Media container {container_id} failed to process in time (account={account})")
    return publish_container(container_id, account=account)


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


def create_carousel_item_container(image_url: str, account: str = "en", max_retries: int = 3) -> str:
    """
    Create one child item of a carousel. Same idea as create_media_container,
    but marked is_carousel_item and - per the Graph API - must NOT include
    a caption (the caption belongs to the parent carousel container only).

    Retries with backoff on the "media download failed" error specifically
    (see _is_transient_media_fetch_error) since that's usually a CDN
    propagation race, not a real problem with the file - everything else
    still fails immediately.
    """
    _check_env(account)
    for attempt in range(1, max_retries + 1):
        resp = requests.post(
            f"{GRAPH_BASE}/{_user_id(account)}/media",
            data={
                "image_url": image_url,
                "is_carousel_item": "true",
                "access_token": _access_token(account),
            },
            timeout=30,
        )
        try:
            _raise_with_detail(resp)
            return resp.json()["id"]
        except requests.HTTPError as e:
            if attempt < max_retries and _is_transient_media_fetch_error(e):
                wait = 5 * attempt  # 5s, 10s, ...
                print(f"[instagram_publish] media fetch failed for {image_url} (account={account}) "
                      f"(likely CDN propagation lag) - retrying in {wait}s "
                      f"(attempt {attempt}/{max_retries})")
                time.sleep(wait)
                continue
            raise


def create_carousel_container(child_container_ids: list, caption: str, account: str = "en") -> str:
    """Create the parent CAROUSEL container that references the already-created child items."""
    _check_env(account)
    resp = requests.post(
        f"{GRAPH_BASE}/{_user_id(account)}/media",
        data={
            "media_type": "CAROUSEL",
            "children": ",".join(child_container_ids),
            "caption": caption,
            "access_token": _access_token(account),
        },
        timeout=30,
    )
    _raise_with_detail(resp)
    return resp.json()["id"]


def post_carousel_to_instagram(image_urls: list, caption: str, account: str = "en") -> str:
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
        child_id = create_carousel_item_container(image_url, account=account)
        if not wait_for_container_ready(child_id, account=account):
            raise RuntimeError(f"Carousel item container {child_id} failed to process in time (account={account})")
        child_ids.append(child_id)

    carousel_id = create_carousel_container(child_ids, caption, account=account)
    if not wait_for_container_ready(carousel_id, account=account):
        raise RuntimeError(f"Carousel container {carousel_id} failed to process in time (account={account})")
    return publish_container(carousel_id, account=account)


import re

def find_recent_matching_post(caption: str, account: str = "en", lookback_seconds: int = 600,
                               not_before: float | None = None) -> str | None:
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
    caption matches (in the sense below) and whose timestamp is within
    lookback_seconds of now AND at/after `not_before` (if given). Returns
    that post's media ID if found, else None (meaning it's safe to
    conclude the publish really did fail).

    Matching compares the FULL caption (after skipping a combined/digest
    post's fixed boilerplate intro line - see match_body() below), not a
    truncated prefix window. A previous version compared only the first
    200 chars of post-intro text, which is enough to distinguish two
    single-story posts but NOT two digest posts that happen to share the
    same #1 headline (common when the top story hasn't changed between
    two slots the same day) - the truncated window matched purely on
    that shared top story and never got far enough into the caption to
    see that the rest of the story list was completely different,
    producing a false-positive match against an unrelated slot's post.
    Comparing the whole body closes that hole: two posts only match now
    if their entire story list (not just the first headline) is
    identical.

    `not_before`, if given, should be the unix timestamp of when THIS
    publish attempt started (not just "now"). Without it, a match is
    only bounded by lookback_seconds on both sides of "now" - which
    means a genuinely different, unrelated post made a few minutes
    BEFORE this attempt even started (e.g. the previous slot's post)
    can still fall inside the window and match. Requiring the matched
    post's timestamp to be >= not_before rules that out: nothing that
    was already on the feed before this attempt began can be mistaken
    for this attempt's result.
    """
    try:
        resp = requests.get(
            f"{GRAPH_BASE}/{_user_id(account)}/media",
            params={
                "fields": "id,caption,timestamp",
                "limit": 10,
                "access_token": _access_token(account),
            },
            timeout=15,
        )
        _raise_with_detail(resp)
    except requests.RequestException as e:
        print(f"[instagram_publish] couldn't verify recent posts for account={account} ({e}) - "
              f"treating the publish as failed")
        return None

    def match_body(text: str) -> str:
        # Skip past a combined/digest post's fixed boilerplate intro
        # (everything up to and including the first "\n\n1. ") before
        # comparing, so the comparison is made of genuinely
        # story-specific text rather than mostly-identical template
        # wording. Falls back to the raw caption if no such intro is
        # present (e.g. single-story posts). No length truncation -
        # the full remaining caption must match, so two digest posts
        # that only share their top headline (but differ further down
        # the list) are correctly treated as different posts.
        skipped = re.sub(r"^.*?\n\n1\.\s*", "", text or "", count=1, flags=re.DOTALL)
        return skipped if skipped != text else (text or "")

    target_body = match_body(caption)
    now = time.time()

    for item in resp.json().get("data", []):
        item_body = match_body(item.get("caption") or "")
        item_ts_str = item.get("timestamp")
        if not item_ts_str or item_body != target_body:
            continue
        try:
            # Instagram timestamps look like "2026-08-03T18:07:41+0000"
            item_ts = time.mktime(time.strptime(item_ts_str[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        if now - item_ts > lookback_seconds:
            continue
        if not_before is not None and item_ts < not_before:
            continue
        return item["id"]

    return None


if __name__ == "__main__":
    print("Set IG_USER_ID / IG_ACCESS_TOKEN (English page) and IG_USER_ID_HI / IG_ACCESS_TOKEN_HI "
          "(Hindi page), then call post_to_instagram(image_url, caption, account='en'|'hi')")
    print("or post_carousel_to_instagram([image_url, ...], caption, account='en'|'hi') for a multi-slide post.")
