"""
content_pregen.py
Runs on the same 30-min cadence as daily_scheduler.py --check-once (see
scheduler.yml), as a step BEFORE the check-and-post step. Builds the
full carousel (images + caption + hashtags) for any slot whose post
time is coming up within BUILD_WINDOW_MINUTES and hasn't been built
yet - NOT the whole day at once.

Why a rolling 30-min-ahead build instead of "everything at midnight":
the schedule (bare timestamps) is announced to the companion PWA right
at midnight, but which actual news stories go into each slot is only
decided shortly before that slot fires. This mirrors how you'd want to
review it - each slot's real content shows up in the app not long
before you'd need to act on it (view manually / let it auto-post), not
a whole day of picks made in a single midnight run.

Each slot's stories are marked "posted" in Supabase (posted_articles)
the moment they're chosen here, NOT when actually published to
Instagram later. This is deliberate: it's what stops one slot's build
from picking the same top headline another slot already claimed
minutes earlier. The trade-off: if a slot's actual Instagram publish
later fails outright (rare - see hourly_run's verify-against-the-real-
feed safety net), that slot's stories are still "spent" and won't be
recycled into a future post. Given how rarely that happens and how bad
true duplicate/near-duplicate posting is for the account, that
trade-off is the right one.

Safe to re-run: any slot that already has content from an earlier
attempt is skipped, so a failed/interrupted run just gets picked back
up on the next 30-min tick without rebuilding or double-reserving
stories.
"""
import traceback
from datetime import datetime

import hourly_run
from daily_scheduler import (
    STORIES_PER_POST, IMAGES_PER_STORY, SLOTS_KEY,
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
        print(f"\n[slot {index + 1}/{len(planned_times)}] planned {iso_time} - building...")
        try:
            result = hourly_run.run_combined(
                story_count=STORIES_PER_POST,
                images_per_story=IMAGES_PER_STORY,
                apply_jitter=False,
                publish=False,  # build + upload + reserve stories, but don't post yet
            )
        except Exception:
            print(f"[slot {index + 1}] build failed - leaving unbuilt, "
                  f"daily_scheduler.py will build it fresh when this slot's time comes.")
            traceback.print_exc()
            failed += 1
            continue

        if not result["results"]:
            print(f"[slot {index + 1}] no usable stories found this attempt - leaving "
                  f"unbuilt, will build fresh at post time instead.")
            failed += 1
            continue

        built_slot = {
            "index": index,
            "planned_time": iso_time,
            "image_urls": result["image_urls"],
            "caption": result["caption"],
            "stories": [
                {"title": r["title"], "source": r["source"], "is_sensitive": r.get("is_sensitive", False)}
                for r in result["results"]
            ],
        }
        # Replace the empty skeleton entry for this index (written at midnight
        # by _publish_schedule_skeleton) in place, rather than appending a
        # second row for the same slot.
        slots_state["slots"] = [s for s in slots_state["slots"] if s.get("index") != index]
        slots_state["slots"].append(built_slot)
        _save_slots_state(slots_state)  # save after each slot - a mid-run interruption loses nothing
        built += 1
        print(f"[slot {index + 1}] built: {len(result['results'])} stories, "
              f"{len(result['image_urls'])} images.")

    print(f"\n[{now_ist().isoformat()}] Content build pass done: {built} built, "
          f"{failed} failed (will retry on a later tick or build fresh at post time).")


if __name__ == "__main__":
    pregenerate_today()
