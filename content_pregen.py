"""
content_pregen.py
Runs on the same 30-min cadence as daily_scheduler.py --check-once (see
scheduler.yml), as a step BEFORE the check-and-post step. Builds
CANDIDATE_STORY_COUNT candidate stories (images + per-story text) for
any slot whose post time is coming up within BUILD_WINDOW_MINUTES and
hasn't been built yet - NOT the whole day at once.

Why more candidates than actually post: the PWA's review screen lets
you pick which STORIES_PER_POST of the candidates go out, and reorder
them, before the slot fires - see save-slot-selection (Supabase Edge
Function). If you never open the review screen, the top STORIES_PER_POST
candidates (by the same priority/sensitivity order run_combined always
used) are what gets posted, so behavior is unchanged from before this
feature existed.

Why a rolling 30-min-ahead build instead of "everything at midnight":
the schedule (bare timestamps) is announced to the companion PWA right
at midnight, but which actual news stories go into each slot is only
decided shortly before that slot fires. This mirrors how you'd want to
review it - each slot's real content shows up in the app not long
before you'd need to act on it (review it / let it auto-post), not a
whole day of picks made in a single midnight run.

Each candidate's story is marked "posted" in Supabase (posted_articles)
the moment it's built here, NOT when actually published to Instagram
later - and that includes candidates that don't end up selected. This
is deliberate: it's what stops one slot's build from picking the same
top headline another slot already claimed minutes earlier. The
trade-off: an unselected candidate is "spent" and won't be recycled
into a future post either. Given how rarely a slot's publish actually
fails outright (see hourly_run's verify-against-the-real-feed safety
net) and how bad true duplicate/near-duplicate posting is for the
account, that trade-off is the right one.

Safe to re-run: any slot that already has content from an earlier
attempt is skipped, so a failed/interrupted run just gets picked back
up on the next 30-min tick without rebuilding or double-reserving
stories.
"""
import traceback
from datetime import datetime

import hourly_run
from daily_scheduler import (
    STORIES_PER_POST, CANDIDATE_STORY_COUNT, IMAGES_PER_STORY, SLOTS_KEY,
    _ensure_today_schedule_remote, _load_state_remote, today_ist, now_ist,
)
from supabase_client import get_state, save_state

BUILD_WINDOW_MINUTES = 30  # build a slot once its post time is this close (or closer/overdue)


def _load_slots_state() -> dict:
    slots_state = get_state(SLOTS_KEY, default={})
    today_str = today_ist().isoformat()
    if slots_state.get("date") != today_str:
        slots_state = {"date": today_str, "slots": []}
    return slots_state


def _save_slots_state(slots_state: dict):
    save_state(SLOTS_KEY, slots_state)


def _already_built(slots_state: dict, index: int) -> bool:
    return any(s.get("index") == index and s.get("image_urls") for s in slots_state["slots"])


def _slot_from_candidates(candidates: list, selected_ids: list) -> dict:
    """
    Computes the fields daily_scheduler.py actually publishes
    (image_urls/caption/image_urls_hi/caption_hi/stories) from a
    candidates list + an ordered subset of ids to include. This is the
    SAME shape whether the selection is today's untouched AI-priority
    default (computed here, right after building) or something the
    PWA's review screen saved later (computed instead by
    save-slot-selection, which writes the same fields directly to
    Supabase - see that function for why it uses a templated caption
    instead of a fresh AI call).
    """
    by_id = {c["id"]: c for c in candidates}
    selected = [by_id[i] for i in selected_ids if i in by_id]

    image_urls = [u for c in selected for u in c["image_urls"]][:10]
    image_urls_hi = [u for c in selected for u in c.get("image_urls_hi", [])][:10]

    caption_stories = [
        {"title": c["title"], "source": c["source"], "detail_text": c.get("detail_text"),
         "caption_paragraph": c.get("caption_paragraph"), "is_sensitive": c.get("is_sensitive", False)}
        for c in selected
    ]
    caption = hourly_run.build_combined_caption(caption_stories)

    caption_hi = ""
    if image_urls_hi:
        # Same field names build_combined_caption_hindi expects
        # (headline_hi/caption_paragraph_hi/detail_hi) - candidates
        # store the Hindi headline as "title_hi", so map it across.
        # No AI call here: every story's Hindi text was already
        # translated once when its candidate was built (see
        # hourly_run.build_candidates / _build_hindi_slides).
        caption_stories_hi = [
            {"title": c["title"], "source": c["source"], "headline_hi": c.get("title_hi"),
             "caption_paragraph_hi": c.get("caption_paragraph_hi")}
            for c in selected
        ]
        caption_hi = hourly_run.build_combined_caption_hindi(caption_stories_hi)

    stories = [
        {"title": c["title"], "source": c["source"], "is_sensitive": c.get("is_sensitive", False),
         "title_hi": c.get("title_hi", "")}
        for c in selected
    ]
    return {
        "image_urls": image_urls, "caption": caption,
        "image_urls_hi": image_urls_hi, "caption_hi": caption_hi,
        "stories": stories,
    }


def pregenerate_today():
    schedule_state = _ensure_today_schedule_remote(_load_state_remote())
    planned_times = schedule_state["planned_times"]
    slots_state = _load_slots_state()
    now = now_ist()

    due_for_build = [
        (i, iso_time) for i, iso_time in enumerate(planned_times)
        if not _already_built(slots_state, i)
        and (datetime.fromisoformat(iso_time) - now).total_seconds() / 60 <= BUILD_WINDOW_MINUTES
    ]

    if not due_for_build:
        print(f"[{now.isoformat()}] No slot within {BUILD_WINDOW_MINUTES} min of its post "
              f"time that still needs content built.")
        return

    print(f"[{now.isoformat()}] Building content for {len(due_for_build)} slot(s) now "
          f"within the {BUILD_WINDOW_MINUTES}-min build window...")

    built = 0
    failed = 0

    for index, iso_time in due_for_build:
        print(f"\n[slot {index + 1}/{len(planned_times)}] planned {iso_time} - building "
              f"{CANDIDATE_STORY_COUNT} candidates...")
        try:
            result = hourly_run.build_candidates(
                candidate_count=CANDIDATE_STORY_COUNT,
                images_per_story=IMAGES_PER_STORY,
            )
        except Exception:
            print(f"[slot {index + 1}] build failed - leaving unbuilt, "
                  f"daily_scheduler.py will build it fresh when this slot's time comes.")
            traceback.print_exc()
            failed += 1
            continue

        candidates = result["candidates"]
        if not candidates:
            print(f"[slot {index + 1}] no usable stories found this attempt - leaving "
                  f"unbuilt, will build fresh at post time instead.")
            failed += 1
            continue

        selected_ids = [c["id"] for c in candidates[:STORIES_PER_POST]]
        computed = _slot_from_candidates(candidates, selected_ids)

        built_slot = {
            "index": index,
            "planned_time": iso_time,
            "candidates": candidates,
            "selected_story_ids": selected_ids,
            **computed,
        }
        # Replace the empty skeleton entry for this index (written at midnight
        # by _publish_schedule_skeleton) in place, rather than appending a
        # second row for the same slot.
        slots_state["slots"] = [s for s in slots_state["slots"] if s.get("index") != index]
        slots_state["slots"].append(built_slot)
        _save_slots_state(slots_state)  # save after each slot - a mid-run interruption loses nothing
        built += 1
        print(f"[slot {index + 1}] built: {len(candidates)} candidates, "
              f"{len(selected_ids)} selected by default, {len(computed['image_urls'])} images.")

    print(f"\n[{now_ist().isoformat()}] Content build pass done: {built} built, "
          f"{failed} failed (will retry on a later tick or build fresh at post time).")


if __name__ == "__main__":
    pregenerate_today()