"""
content_pregen.py
Runs once a day, right at IST midnight (triggered by a separate GitHub
Actions cron entry - see scheduler.yml). Builds the FULL carousel
(images + caption + hashtags) for every slot in today's schedule up
front, instead of daily_scheduler.py building each one fresh at post
time.

Why: the companion PWA needs to show you tomorrow's whole lineup (all
slide images + captions) as soon as the day starts, and needs a
15-minutes-before notification with real content behind it - which is
only possible if that content already exists well before the post
time, not built in the same few seconds the post fires.

Each slot's stories are marked "posted" in Supabase (posted_articles)
the moment they're chosen here, NOT when actually published to
Instagram later. This is deliberate: it's what stops slot #2's build
from picking the same top headline slot #1 already claimed a few
minutes earlier in this same run. The trade-off: if a slot's actual
Instagram publish later fails outright (rare - see hourly_run's
verify-against-the-real-feed safety net), that slot's stories are
still "spent" and won't be recycled into a future post. Given how
rarely that happens and how bad true duplicate/near-duplicate posting
is for the account, that trade-off is the right one.

Safe to re-run: any slot that already has content from an earlier
attempt today is skipped, so a failed/interrupted run can just be
re-triggered (e.g. via workflow_dispatch) without rebuilding
everything from scratch or double-reserving stories.
"""
import traceback

import hourly_run
from daily_scheduler import (
    STORIES_PER_POST, IMAGES_PER_STORY, SLOTS_KEY,
    _ensure_today_schedule_remote, _load_state_remote, today_ist, now_ist,
)
from supabase_client import get_state, save_state


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

    print(f"[{now_ist().isoformat()}] Pre-generating content for {len(planned_times)} "
          f"slot(s) planned today ({today_ist().isoformat()})...")

    built = 0
    skipped = 0
    failed = 0

    for index, iso_time in enumerate(planned_times):
        if _already_built(slots_state, index):
            skipped += 1
            continue

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

        slots_state["slots"].append({
            "index": index,
            "planned_time": iso_time,
            "image_urls": result["image_urls"],
            "caption": result["caption"],
            "stories": [
                {"title": r["title"], "source": r["source"], "is_sensitive": r.get("is_sensitive", False)}
                for r in result["results"]
            ],
        })
        _save_slots_state(slots_state)  # save after each slot - a mid-run interruption loses nothing
        built += 1
        print(f"[slot {index + 1}] built: {len(result['results'])} stories, "
              f"{len(result['image_urls'])} images.")

    print(f"\n[{now_ist().isoformat()}] Pre-generation done: {built} built, "
          f"{skipped} already-built (skipped), {failed} failed (will build fresh at post time).")


if __name__ == "__main__":
    pregenerate_today()
