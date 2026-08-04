"""
daily_scheduler.py
Long-running process that posts somewhere between MIN_POSTS_PER_DAY and
MAX_POSTS_PER_DAY times a day, at random times biased toward the hours
when Instagram audiences are generally most active - never on a fixed
:00/:30 grid, and never bunched together (a minimum gap is enforced
between consecutive posts).

Each individual post still goes through hourly_run.run_combined(), which:
  - surfaces news from many query angles (top headlines + explicit
    "breaking news" + topical buckets + direct publisher feeds,
    domestic and global), ranked best-to-worst by priority_score
  - bundles the 5 highest-priority not-yet-posted stories into ONE
    carousel post: 2 images per story (hook + detail) = 10 images,
    Instagram's per-carousel cap, in priority order
  - never reposts a story - checked both by exact link AND by fuzzy
    title match, so the same event covered by a different outlet under
    a reworded headline still gets caught
  - writes one AI-generated round-up caption naming all 5 stories in
    rank order, plus a merged hashtag set (10-15 tags)
  - draws a red "BREAKING" badge on a story's slides ONLY when that
    story is actually flagged as breaking

Run this as your one long-lived process instead of an hourly cron job:
    python daily_scheduler.py

State (today's planned times + which have fired) is persisted to
scheduler_state.json next to this file, so a restart mid-day resumes
correctly instead of re-planning (and potentially over-posting) or
silently skipping the rest of the day.
"""
import argparse
import json
import os
import random
import time
import traceback
from datetime import datetime, timedelta, date, timezone

import hourly_run
from supabase_client import get_state, save_state

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler_state.json")
STATE_KEY = "scheduler_state"  # Supabase app_state key, used by check_once()
SLOTS_KEY = "daily_slots"  # Supabase app_state key holding pre-generated content, written by content_pregen.py

# GitHub Actions runners (and most servers) run on UTC. Everything in
# this file - "today", the PEAK_WINDOWS clock hours, "midnight" - is
# meant to mean India Standard Time, not the server's own clock, so all
# of it is anchored to this fixed offset rather than naive datetime.now().
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()

MIN_POSTS_PER_DAY = 3
MAX_POSTS_PER_DAY = 5

# Each "post" now bundles this many distinct stories into ONE combined
# carousel (see hourly_run.run_combined) instead of one story per post -
# so MIN/MAX_POSTS_PER_DAY x STORIES_PER_POST is the real daily story
# throughput (e.g. 13-23 posts x 5 stories = 65-115 stories/day).
STORIES_PER_POST = 5
IMAGES_PER_STORY = 2

# Minimum gap enforced between any two consecutive posts, so a busy
# random draw can't accidentally cluster several posts back-to-back
# (which would read as spammy no matter how the times were chosen).
MIN_GAP_MINUTES = 25

# How often the main loop wakes up to check whether it's time to post.
# Coarse enough to be cheap, fine enough that posts fire within a minute
# of their planned time.
POLL_SECONDS = 30

# Windows (24h local time) when engagement tends to be highest, each with
# a relative weight controlling how much of the day's post budget lands
# there. These are general, widely-cited social engagement patterns, not
# an exact science - the point is "mostly during active hours, never all
# bunched at 3am," not minute-perfect optimization.
PEAK_WINDOWS = [
    # (start_hour, start_min, end_hour, end_min, weight)
    (7, 0, 9, 30, 3),     # morning commute / breakfast scrolling
    (12, 0, 14, 0, 2),    # lunch break
    (17, 0, 19, 0, 3),    # evening commute
    (19, 0, 22, 30, 5),   # prime time - dinner through late evening, highest weight
    (22, 30, 23, 45, 1),  # late-night scrollers, light coverage
]


def _random_time_in_window(day: date, window) -> datetime:
    sh, sm, eh, em, _ = window
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, eh, em, tzinfo=IST)
    delta_seconds = int((end - start).total_seconds())
    return start + timedelta(seconds=random.randint(0, max(delta_seconds, 0)))


def generate_daily_schedule(day: date = None, min_posts: int = MIN_POSTS_PER_DAY,
                             max_posts: int = MAX_POSTS_PER_DAY,
                             min_gap_minutes: int = MIN_GAP_MINUTES) -> list:
    """
    Returns a sorted list of IST-aware datetime objects for `day`,
    weighted toward PEAK_WINDOWS (which are IST clock hours), with at
    least min_gap_minutes between consecutive posts.
    """
    day = day or today_ist()
    num_posts = random.randint(min_posts, max_posts)

    weights = [w[4] for w in PEAK_WINDOWS]

    times = []
    max_tries_total = num_posts * 200  # generous ceiling so we never spin forever
    tries = 0
    while len(times) < num_posts and tries < max_tries_total:
        tries += 1
        window = random.choices(PEAK_WINDOWS, weights=weights, k=1)[0]
        candidate = _random_time_in_window(day, window)
        if all(abs((candidate - t).total_seconds()) >= min_gap_minutes * 60 for t in times):
            times.append(candidate)

    times.sort()
    return times


def _load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def _ensure_today_schedule(state: dict) -> dict:
    today_str = date.today().isoformat()
    if state.get("date") != today_str:
        planned = generate_daily_schedule(date.today())
        state = {
            "date": today_str,
            "planned_times": [t.isoformat() for t in planned],
            "posted": [False] * len(planned),
        }
        _save_state(state)
        print(f"[{datetime.now().isoformat()}] New schedule for {today_str}: "
              f"{len(planned)} posts planned at "
              f"{', '.join(t.strftime('%H:%M') for t in planned)}")
    else:
        remaining = state["posted"].count(False)
        upcoming = [
            datetime.fromisoformat(t).strftime('%H:%M')
            for t, posted in zip(state["planned_times"], state["posted"])
            if not posted
        ]
        print(f"[{datetime.now().isoformat()}] Resuming today's existing schedule "
              f"({len(state['posted']) - remaining}/{len(state['posted'])} already posted). "
              f"Remaining: {', '.join(upcoming) if upcoming else '(none - done for today)'}")
    return state


def _load_state_remote() -> dict:
    return get_state(STATE_KEY, default={})


def _save_state_remote(state: dict):
    save_state(STATE_KEY, state)


def _ensure_today_schedule_remote(state: dict) -> dict:
    today_str = today_ist().isoformat()
    if state.get("date") != today_str:
        planned = generate_daily_schedule(today_ist())
        state = {
            "date": today_str,
            "planned_times": [t.isoformat() for t in planned],
            "posted": [False] * len(planned),
            "notified": [False] * len(planned),
        }
        _save_state_remote(state)
        print(f"[{now_ist().isoformat()}] New schedule for {today_str} (IST): "
              f"{len(planned)} posts planned at "
              f"{', '.join(t.strftime('%H:%M') for t in planned)}")
    elif "notified" not in state:
        state["notified"] = [False] * len(state["planned_times"])
        _save_state_remote(state)
    return state


def _get_prebuilt_slot(due_index: int):
    """
    Looks up content_pregen.py's output for today's slot `due_index`, if
    it exists. Returns the slot dict ({"image_urls": [...], "caption":
    str, "stories": [...]}), or None if pregeneration hasn't produced
    this slot yet (e.g. content_pregen.py didn't run today, or is still
    mid-way through building slots) - in which case check_once() falls
    back to building fresh, exactly like before this feature existed.
    """
    slots_state = get_state(SLOTS_KEY, default={})
    if slots_state.get("date") != today_ist().isoformat():
        return None
    for slot in slots_state.get("slots", []):
        if slot.get("index") == due_index and slot.get("image_urls"):
            return slot
    return None


def _publish_prebuilt_slot(slot: dict):
    """Publishes a slot's already-built content (images already uploaded,
    stories already reserved in Supabase by content_pregen.py) instead of
    building anything fresh. Reuses the same 'verify against the real IG
    feed if the API call errors' safety net as hourly_run.run_combined."""
    from instagram_publish import (
        post_carousel_to_instagram, post_to_instagram, find_recent_matching_post,
    )

    image_urls = slot["image_urls"]
    caption = slot["caption"]
    print(f"  -> publishing pre-built content ({len(slot.get('stories', []))} stories, "
          f"{len(image_urls)} images)...")
    try:
        if len(image_urls) >= 2:
            media_id = post_carousel_to_instagram(image_urls, caption)
        else:
            media_id = post_to_instagram(image_urls[0], caption)
        print(f"  -> posted. Media ID: {media_id}")
    except Exception as e:
        print(f"  -> Instagram publish failed: {e}")
        print("  -> checking the account's recent media in case it actually posted "
              "despite the error...")
        media_id = find_recent_matching_post(caption)
        if media_id:
            print(f"  -> confirmed: post {media_id} actually went live despite the error.")
        else:
            print("  -> confirmed: it genuinely did not post. The stories in this slot "
                  "were already reserved at pre-generation time, so they won't be "
                  "retried automatically - check the app/logs and post manually if needed.")


def check_once():
    """
    One-shot version of run_forever()'s loop body, meant to be invoked by
    an external scheduler (GitHub Actions cron, e.g. every 20 minutes)
    instead of running as a resident process. State lives in Supabase
    (app_state, key "scheduler_state") rather than a local JSON file,
    since GitHub Actions runners don't persist a filesystem between runs.
    All "today"/"now" here means IST, regardless of the server's own
    clock.

    Fires AT MOST ONE post per invocation - the single earliest overdue,
    not-yet-posted slot that also clears MIN_GAP_MINUTES since the last
    post - then exits. Safe to call as often as you like; it's a no-op
    if nothing is currently due.

    If content_pregen.py already built this slot's carousel earlier
    today (the companion app's "all content ready at midnight" feature),
    that pre-built content is published as-is. Otherwise this builds a
    fresh carousel on the spot, exactly as before that feature existed.
    """
    state = _load_state_remote()
    state = _ensure_today_schedule_remote(state)
    now = now_ist()

    due_index = None
    for i, iso_time in enumerate(state["planned_times"]):
        if state["posted"][i]:
            continue
        if now >= datetime.fromisoformat(iso_time):
            due_index = i
            break

    if due_index is None:
        print(f"[{now.isoformat()}] Nothing due right now. "
              f"{sum(state['posted'])}/{len(state['posted'])} posted today.")
        return

    last_post_iso = state.get("last_post_time")
    last_post_dt = datetime.fromisoformat(last_post_iso) if last_post_iso else None
    gap_ok = last_post_dt is None or (now - last_post_dt).total_seconds() >= MIN_GAP_MINUTES * 60

    if not gap_ok:
        print(f"[{now.isoformat()}] Slot #{due_index + 1} is overdue but the "
              f"minimum gap since the last post hasn't elapsed yet - waiting for a later run.")
        return

    planned_dt = datetime.fromisoformat(state["planned_times"][due_index])
    print(f"[{now.isoformat()}] Firing scheduled post "
          f"#{due_index + 1}/{len(state['planned_times'])} "
          f"(planned {planned_dt.strftime('%H:%M')} IST)")
    try:
        prebuilt = _get_prebuilt_slot(due_index)
        if prebuilt:
            _publish_prebuilt_slot(prebuilt)
        else:
            print("  -> no pre-built content found for this slot - building fresh now.")
            # Timing/randomness is already handled by the schedule itself,
            # so skip hourly_run's own extra jitter delay.
            hourly_run.run_combined(
                story_count=STORIES_PER_POST,
                images_per_story=IMAGES_PER_STORY,
                apply_jitter=False,
            )
    except Exception:
        print("Post attempt failed - will not retry this slot, "
              "continuing with the rest of today's schedule.")
        traceback.print_exc()

    state["posted"][due_index] = True
    state["last_post_time"] = now_ist().isoformat()
    _save_state_remote(state)


def run_forever():
    state = _load_state()
    state = _ensure_today_schedule(state)
    print(f"[{datetime.now().isoformat()}] Scheduler is running. Checking every "
          f"{POLL_SECONDS}s for a due post. Leave this running; it prints again "
          f"when it actually fires a post, and every ~30 minutes as a heartbeat "
          f"so you know it's still alive.")
    last_heartbeat = time.time()
    HEARTBEAT_SECONDS = 1800

    while True:
        if state.get("date") != date.today().isoformat():
            state = _ensure_today_schedule(state)  # only re-plans on an actual day rollover
        now = datetime.now()

        # Only ever fire the SINGLE earliest overdue, not-yet-posted slot
        # per pass - not every overdue slot in one go. If the process
        # started late (or is catching up after being stopped for a
        # while), there may be several slots simultaneously "due"; firing
        # them all back-to-back would blow through MIN_GAP_MINUTES, which
        # is exactly what was happening before this fix.
        due_index = None
        for i, iso_time in enumerate(state["planned_times"]):
            if state["posted"][i]:
                continue
            if now >= datetime.fromisoformat(iso_time):
                due_index = i
                break

        if due_index is not None:
            last_post_iso = state.get("last_post_time")
            last_post_dt = datetime.fromisoformat(last_post_iso) if last_post_iso else None
            gap_ok = last_post_dt is None or (now - last_post_dt).total_seconds() >= MIN_GAP_MINUTES * 60

            if gap_ok:
                planned_dt = datetime.fromisoformat(state["planned_times"][due_index])
                print(f"[{datetime.now().isoformat()}] Firing scheduled post "
                      f"#{due_index + 1}/{len(state['planned_times'])} "
                      f"(planned {planned_dt.strftime('%H:%M')})")
                try:
                    # Timing/randomness is already handled by the schedule
                    # itself, so skip hourly_run's own extra jitter delay.
                    hourly_run.run_combined(
                        story_count=STORIES_PER_POST,
                        images_per_story=IMAGES_PER_STORY,
                        apply_jitter=False,
                    )
                except Exception:
                    print("Post attempt failed - will not retry this slot, "
                          "continuing with the rest of today's schedule.")
                    traceback.print_exc()
                state["posted"][due_index] = True
                state["last_post_time"] = datetime.now().isoformat()
                _save_state(state)
            # else: a slot is overdue but we posted too recently - wait
            # for the next poll instead of firing early. It'll fire as
            # soon as the real-world gap since the last post is satisfied.

        if time.time() - last_heartbeat >= HEARTBEAT_SECONDS:
            remaining = state["posted"].count(False)
            print(f"[{datetime.now().isoformat()}] Still running - "
                  f"{remaining} post(s) left today.")
            last_heartbeat = time.time()

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check-once", action="store_true",
        help="Run one check-and-maybe-post pass against Supabase-backed state, then exit. "
             "Use this mode when invoked by an external scheduler like GitHub Actions cron. "
             "Without this flag, runs forever as a resident process using local "
             "scheduler_state.json (only viable on a machine that stays on 24/7).",
    )
    args = parser.parse_args()

    if args.check_once:
        check_once()
    else:
        run_forever()
