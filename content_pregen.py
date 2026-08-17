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

Can also be pointed at ONE specific slot with `--slot INDEX`, ignoring
BUILD_WINDOW_MINUTES entirely - this is what the PWA's "Generate now"
button (generate-slot.yml, dispatched via the generate-slot Supabase
Edge Function) uses so a reviewer doesn't have to wait for a slot to
enter the rolling 30-min-ahead window before its content shows up.
This only builds content; it never posts anything and never touches
planned_time, so the slot still only goes out at its normal fixed time
via daily_scheduler.py --check-once, same as every other slot.
"""
import argparse
import traceback
from datetime import datetime

import hourly_run
from daily_scheduler import (
    STORIES_PER_POST, CANDIDATE_STORY_COUNT, IMAGES_PER_STORY, slots_key,
    _ensure_today_schedule_remote, _load_state_remote, today_ist, now_ist,
)
from supabase_client import get_state, save_state

BUILD_WINDOW_MINUTES = 30  # build a slot once its post time is this close (or closer/overdue)


def _load_slots_state() -> dict:
    today_str = today_ist().isoformat()
    slots_state = get_state(slots_key(today_str), default={})
    if slots_state.get("date") != today_str:
        slots_state = {"date": today_str, "slots": []}
    return slots_state


def _save_slots_state(slots_state: dict):
    save_state(slots_key(slots_state["date"]), slots_state)


def _already_built(slots_state: dict, index: int) -> bool:
    return any(s.get("index") == index and s.get("image_urls") for s in slots_state["slots"])


def _slot_from_candidates(candidates: list, selected_ids: list,
                           hook_slide_url: str = "", hook_slide_url_hi: str = "",
                           follow_slide_url: str = "", follow_slide_url_hi: str = "") -> dict:
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

    hook_slide_url(_hi)/follow_slide_url(_hi): the ultimate-hook collage
    and follow-for-more end card built/uploaded once in
    hourly_run.build_candidates (see its docstring for why the hook
    slide doesn't get regenerated if the reviewer later swaps a
    candidate). Prepended/appended here exactly like run_combined()
    does with its own local-path equivalents, so a pre-generated post
    ends up with the identical slide-1/slide-N bookends a run_combined()
    fallback post would have had. Blank strings (not yet available, or
    Hindi disabled) are skipped rather than inserted as empty slides.
    """
    by_id = {c["id"]: c for c in candidates}
    selected = [by_id[i] for i in selected_ids if i in by_id]

    story_image_urls = [u for c in selected for u in c["image_urls"]]
    story_image_urls_hi = [u for c in selected for u in c.get("image_urls_hi", [])]

    # Instagram's 10-image carousel cap: reserve 1 slot for the hook
    # collage and 1 for the follow-for-more card (when present), same
    # budgeting hourly_run.run_combined uses.
    def _assemble(hook_url, story_urls, follow_url):
        bookends = (1 if hook_url else 0) + (1 if follow_url else 0)
        budget = 10 - bookends
        parts = ([hook_url] if hook_url else []) + story_urls[:budget] + ([follow_url] if follow_url else [])
        return parts

    image_urls = _assemble(hook_slide_url, story_image_urls, follow_slide_url)
    image_urls_hi = _assemble(hook_slide_url_hi, story_image_urls_hi, follow_slide_url_hi)

    caption_stories = [
        {"title": c["title"], "source": c["source"], "detail_text": c.get("detail_text"),
         "caption_paragraph": c.get("caption_paragraph"), "is_sensitive": c.get("is_sensitive", False)}
        for c in selected
    ]
    caption = hourly_run.build_combined_caption(caption_stories)

    caption_hi = ""
    if story_image_urls_hi:
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


def _build_one_slot(index: int, iso_time: str, total_slots: int, slots_state: dict) -> bool:
    """
    Builds CANDIDATE_STORY_COUNT candidates for ONE slot and saves the
    result into slots_state in place. Shared by the rolling windowed
    pass (pregenerate_today) and the manual single-slot trigger
    (force_build_slot) so both build/save exactly the same shape.
    Returns True if the slot ended up with content, False if the build
    failed or found no usable stories - either way the slot is left
    unbuilt so it gets retried on a later tick or built fresh at post
    time, never left half-written.
    """
    print(f"\n[slot {index + 1}/{total_slots}] planned {iso_time} - building "
          f"{CANDIDATE_STORY_COUNT} candidates...")
    try:
        result = hourly_run.build_candidates(
            candidate_count=CANDIDATE_STORY_COUNT,
            images_per_story=IMAGES_PER_STORY,
            default_selected_count=STORIES_PER_POST,
        )
    except Exception:
        print(f"[slot {index + 1}] build failed - leaving unbuilt, "
              f"will build fresh when this slot's time comes.")
        traceback.print_exc()
        return False

    candidates = result["candidates"]
    if not candidates:
        print(f"[slot {index + 1}] no usable stories found this attempt - leaving "
              f"unbuilt, will build fresh at post time instead.")
        return False

    hook_slide_url = result.get("hook_slide_url", "")
    hook_slide_url_hi = result.get("hook_slide_url_hi", "")
    follow_slide_url = result.get("follow_slide_url", "")
    follow_slide_url_hi = result.get("follow_slide_url_hi", "")

    selected_ids = [c["id"] for c in candidates[:STORIES_PER_POST]]
    computed = _slot_from_candidates(
        candidates, selected_ids,
        hook_slide_url=hook_slide_url, hook_slide_url_hi=hook_slide_url_hi,
        follow_slide_url=follow_slide_url, follow_slide_url_hi=follow_slide_url_hi,
    )

    built_slot = {
        "index": index,
        "planned_time": iso_time,
        "candidates": candidates,
        "selected_story_ids": selected_ids,
        # Persisted so save-slot-selection (Edge Function) can rebuild
        # image_urls/image_urls_hi later - after a reviewer changes the
        # selection - without needing to (and being unable to, as a
        # Deno function) regenerate the collage/end-card images itself.
        "hook_slide_url": hook_slide_url,
        "hook_slide_url_hi": hook_slide_url_hi,
        "follow_slide_url": follow_slide_url,
        "follow_slide_url_hi": follow_slide_url_hi,
        **computed,
    }
    # Replace the empty skeleton entry for this index (written at midnight
    # by _publish_schedule_skeleton) in place, rather than appending a
    # second row for the same slot.
    slots_state["slots"] = [s for s in slots_state["slots"] if s.get("index") != index]
    slots_state["slots"].append(built_slot)
    _save_slots_state(slots_state)  # save right away - a mid-run interruption loses nothing
    print(f"[slot {index + 1}] built: {len(candidates)} candidates, "
          f"{len(selected_ids)} selected by default, {len(computed['image_urls'])} images.")
    return True


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
        if _build_one_slot(index, iso_time, len(planned_times), slots_state):
            built += 1
        else:
            failed += 1

    print(f"\n[{now_ist().isoformat()}] Content build pass done: {built} built, "
          f"{failed} failed (will retry on a later tick or build fresh at post time).")


def force_build_slot(index: int) -> bool:
    """
    Manual "Generate now" trigger from the PWA - builds ONE specific
    slot's content immediately, ignoring BUILD_WINDOW_MINUTES, instead
    of waiting for it to roll into the normal 30-min-ahead window.
    Never posts anything and never touches planned_time: the slot still
    only actually goes out via the normal daily_scheduler.py
    --check-once run once its fixed time arrives, exactly like every
    other slot.
    """
    schedule_state = _ensure_today_schedule_remote(_load_state_remote())
    planned_times = schedule_state["planned_times"]

    if index < 0 or index >= len(planned_times):
        print(f"Slot index {index} is out of range - today has {len(planned_times)} slot(s).")
        return False

    slots_state = _load_slots_state()
    if _already_built(slots_state, index):
        print(f"Slot {index} already has content built - nothing to do.")
        return True

    ok = _build_one_slot(index, planned_times[index], len(planned_times), slots_state)
    if not ok:
        print(f"Slot {index} couldn't be built on this attempt - it'll retry automatically "
              f"on the next scheduler tick, and again at post time if it's still unbuilt then.")
    return ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--slot", type=int, default=None,
        help="Force-build ONE slot by its 0-based index right now, ignoring the "
             "normal 30-min build window (used by the PWA's 'Generate now' button).",
    )
    args = parser.parse_args()

    if args.slot is not None:
        ok = force_build_slot(args.slot)
        raise SystemExit(0 if ok else 1)

    pregenerate_today()