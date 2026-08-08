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

The companion PWA can request changes to today's schedule (total post
count, and/or specific times for individual not-yet-posted slots) - see
schedule_overrides / slot_overrides in supabase_app_additions.sql.
check_once() fetches and applies any pending override at the start of
every run, before checking what's due.

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
from supabase_client import (
    get_state,
    save_state,
    get_manual_slot_indices,
    get_schedule_override,
)

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler_state.json")
STATE_KEY = "scheduler_state"  # Supabase app_state key, used by check_once()
SLOTS_KEY = "daily_slots"      # Supabase app_state key holding pre-generated content, written by content_pregen.py

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

# Hard ceiling on how many posts a day can be bumped up to via the PWA's
# schedule editor, applied server-side regardless of what the PWA sends -
# the <input max> in the app is just a UI nicety, this is what's actually
# enforced.
MAX_TARGET_POSTS = 25

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
    (7, 0, 9, 30, 3),      # morning commute / breakfast scrolling
    (12, 0, 14, 0, 2),     # lunch break
    (17, 0, 19, 0, 3),     # evening commute
    (19, 0, 22, 30, 5),    # prime time - dinner through late evening, highest weight
    (22, 30, 23, 45, 1),   # late-night scrollers, light coverage
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


def _weighted_time_in_remaining_day(day: date, not_before: datetime, existing_times: list,
                                     min_gap_minutes: int = MIN_GAP_MINUTES,
                                     max_tries: int = 300):
    """
    Draws one new weighted-random time from PEAK_WINDOWS, restricted to
    the portion of each window still ahead of `not_before`, and
    respecting min_gap_minutes against every time already in
    `existing_times`. Returns None if no valid slot could be found
    within max_tries (e.g. the day's remaining peak windows are already
    packed) - the caller treats that as "no more room today."
    """
    candidate_windows = []
    weights = []
    for sh, sm, eh, em, weight in PEAK_WINDOWS:
        start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
        end = datetime(day.year, day.month, day.day, eh, em, tzinfo=IST)
        if end <= not_before:
            continue  # window is already fully in the past
        start = max(start, not_before)
        if start >= end:
            continue
        candidate_windows.append((start, end))
        weights.append(weight)

    if not candidate_windows:
        return None

    tries = 0
    while tries < max_tries:
        tries += 1
        start, end = random.choices(candidate_windows, weights=weights, k=1)[0]
        delta_seconds = int((end - start).total_seconds())
        candidate = start + timedelta(seconds=random.randint(0, max(delta_seconds, 0)))
        if all(abs((candidate - t).total_seconds()) >= min_gap_minutes * 60 for t in existing_times):
            return candidate
    return None


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


def _publish_schedule_skeleton(today_str: str, planned: list):
    """
    Pushes JUST the time slots (no content yet) to app_state[SLOTS_KEY] the
    moment today's schedule is decided, so the companion PWA can show the
    whole day's timestamps immediately at midnight. Each slot's actual
    carousel (image_urls/caption/stories) gets filled in later, ~30 min
    before that slot's own post time - see content_pregen.py.
    """
    save_state(SLOTS_KEY, {
        "date": today_str,
        "slots": [
            {"index": i, "planned_time": t.isoformat(), "image_urls": [], "caption": "", "stories": []}
            for i, t in enumerate(planned)
        ],
    })


def _sync_slots_skeleton(state: dict):
    """
    Re-syncs app_state[SLOTS_KEY] (the PWA's display) with the current
    planned_times after a schedule override changed the slot count or
    times - adds skeleton entries for newly-added slots, drops entries
    for slots that were removed, and updates planned_time on edited
    ones. Leaves already-built content (image_urls/caption/stories) on
    every other slot untouched.

    Best-effort: failure here never blocks posting - it just means the
    app's display lags behind until the next successful sync.
    """
    try:
        slots_state = get_state(SLOTS_KEY, default={})
        if slots_state.get("date") != state["date"]:
            _publish_schedule_skeleton(
                state["date"], [datetime.fromisoformat(t) for t in state["planned_times"]]
            )
            return

        by_index = {s["index"]: s for s in slots_state.get("slots", [])}
        new_slots = []
        for i, iso_time in enumerate(state["planned_times"]):
            existing = by_index.get(i)
            if existing:
                existing["planned_time"] = iso_time
                new_slots.append(existing)
            else:
                new_slots.append({"index": i, "planned_time": iso_time, "image_urls": [], "caption": "", "stories": []})
        slots_state["slots"] = new_slots
        save_state(SLOTS_KEY, slots_state)
    except Exception as e:
        print(f"[{now_ist().isoformat()}] Schedule override: failed to sync the daily_slots "
              f"skeleton (non-fatal - app display may lag, will retry next check): {e}")


def _apply_target_count(state: dict, target_count) -> bool:
    """
    Grows or shrinks today's schedule to `target_count` slots, in place
    on `state`. Returns True if state actually changed.

    - target_count is clamped to [posted_count, MAX_TARGET_POSTS] - never
      drops below however many are already posted, and MAX_TARGET_POSTS
      is a hard server-side ceiling regardless of what the PWA sent.
    - Shrinking removes not-yet-posted slots from the end only (highest
      index first), so it never renumbers a slot that's already posted
      or has an existing time edit/Manual flag pointing at it.
    - Growing appends new slots at the end with freshly drawn
      weighted-random times, respecting MIN_GAP_MINUTES against every
      slot already planned, and only within the time remaining today.
      If there isn't enough room left before midnight to fit every
      requested slot without breaking the gap rule, it adds as many as
      will fit and logs how many it dropped - it won't cram them in.
    - Added/removed slots are NOT re-sorted into planned_times - they're
      appended/removed by index, so the array itself can be
      non-chronological after this. The posting loop scans by index and
      fires whichever due slot it hits first, so in the rare case a
      newly-added slot lands earlier than an existing later-indexed
      slot, order of firing could be slightly off. Not a practical issue
      at typical daily volumes.
    """
    if target_count is None:
        return False

    try:
        target_count = int(target_count)
    except (TypeError, ValueError):
        return False

    posted_count = state["posted"].count(True)
    current_count = len(state["planned_times"])
    target_count = max(posted_count, min(target_count, MAX_TARGET_POSTS))

    if target_count == current_count:
        return False

    if target_count < current_count:
        remove_n = current_count - target_count
        removable_indices = [i for i in range(current_count - 1, -1, -1) if not state["posted"][i]]
        to_remove = sorted(removable_indices[:remove_n], reverse=True)
        for i in to_remove:
            del state["planned_times"][i]
            del state["posted"][i]
            if "notified" in state and i < len(state["notified"]):
                del state["notified"][i]
        print(f"[{now_ist().isoformat()}] Schedule override: removed {len(to_remove)} "
              f"not-yet-posted slot(s), now {len(state['planned_times'])} planned today.")
        return len(to_remove) > 0

    # growing
    add_n = target_count - current_count
    day = today_ist()
    now = now_ist()
    existing_times = [datetime.fromisoformat(t) for t in state["planned_times"]]
    added = 0
    for _ in range(add_n):
        candidate = _weighted_time_in_remaining_day(day, now, existing_times)
        if candidate is None:
            break
        existing_times.append(candidate)
        state["planned_times"].append(candidate.isoformat())
        state["posted"].append(False)
        if "notified" in state:
            state["notified"].append(False)
        added += 1

    dropped = add_n - added
    print(f"[{now_ist().isoformat()}] Schedule override: added {added} new slot(s) "
          f"(requested {add_n})"
          f"{f' - could not fit {dropped} more before midnight without breaking the gap rule' if dropped else ''}. "
          f"Now {len(state['planned_times'])} planned today.")
    return added > 0


def _apply_time_edits(state: dict, time_edits: dict) -> bool:
    """
    Applies specific time changes to not-yet-posted slots. time_edits is
    keyed by slot index (string, since it comes from JSON) -> "HH:MM" in
    IST. Posted slots are left untouched even if an edit is present for
    them. Returns True if anything actually changed.
    """
    if not time_edits:
        return False

    day = today_ist()
    changed = False
    for key, hhmm in time_edits.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(state["planned_times"]):
            continue
        if state["posted"][index]:
            continue
        try:
            hour_str, minute_str = str(hhmm).split(":")
            new_time = datetime(day.year, day.month, day.day, int(hour_str), int(minute_str), tzinfo=IST)
        except (ValueError, TypeError):
            print(f"[{now_ist().isoformat()}] Schedule override: skipping unparseable "
                  f"time edit for slot #{index + 1}: {hhmm!r}")
            continue
        if datetime.fromisoformat(state["planned_times"][index]) != new_time:
            state["planned_times"][index] = new_time.isoformat()
            changed = True

    if changed:
        print(f"[{now_ist().isoformat()}] Schedule override: applied time edit(s).")
    return changed


def _apply_schedule_override(state: dict, override: dict):
    """
    Applies a fetched schedule_overrides row (target_count and/or
    time_edits) to `state` in place, then pushes the result to Supabase
    (scheduler_state) and re-syncs the PWA's daily_slots display - all
    from a single fetched override, so this only ever does one round of
    Supabase writes per check_once() call regardless of how many fields
    changed. Any failure here is caught by the caller and never blocks
    the actual posting check that follows.
    """
    changed_count = _apply_target_count(state, override.get("target_count"))
    changed_times = _apply_time_edits(state, override.get("time_edits") or {})
    if changed_count or changed_times:
        _save_state_remote(state)
        _sync_slots_skeleton(state)


def _ensure_today_schedule_remote(state: dict) -> dict:
    today_str = today_ist().isoformat()
    if state.get("date") != today_str:
        planned = generate_daily_schedule(today_ist())
        state = {
            "date": today_str,
            "planned_times": [t.isoformat() for t in planned],
            "posted": [False] * len(planned),
            "notified": [False] * len(planned),
            "schedule_announced": False,  # flips True once send_notifications.py has pushed the "schedule's ready" alert
        }
        _save_state_remote(state)
        _publish_schedule_skeleton(today_str, planned)
        print(f"[{now_ist().isoformat()}] New schedule for {today_str} (IST): "
              f"{len(planned)} posts planned at "
              f"{', '.join(t.strftime('%H:%M') for t in planned)}")
    else:
        changed = False
        if "notified" not in state:
            state["notified"] = [False] * len(state["planned_times"])
            changed = True
        if "schedule_announced" not in state:
            state["schedule_announced"] = False
            changed = True
        if changed:
            _save_state_remote(state)
    return state


def _get_prebuilt_slot(due_index: int):
    """
    Looks up content_pregen.py's output for today's slot `due_index`, if
    it exists. Returns the slot dict ({"image_urls": [...], "caption":
    str, "stories": [...]}), or None if pregeneration hasn't produced
    this slot yet - in which case check_once() falls back to building
    fresh, exactly like before this feature existed.
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


def _check_manual_slot(state: dict, index: int):
    """A slot you've flagged Manual is overdue - see if it already went
    live on Instagram (you posted it yourself) and, if so, mark it
    posted so the scheduler and the app both stop treating it as
    pending. If nothing matching is found yet, leaves it alone; it'll
    be checked again on the next tick."""
    from instagram_publish import find_recent_matching_post

    slot = _get_prebuilt_slot(index)
    if not slot or not slot.get("caption"):
        return  # content for this slot hasn't been built yet - nothing to match against

    media_id = find_recent_matching_post(slot["caption"])
    if media_id:
        print(f"  -> slot #{index + 1} is flagged Manual and a matching post "
              f"({media_id}) was found on the feed - marking it posted.")
        state["posted"][index] = True
        state["last_post_time"] = now_ist().isoformat()
        _save_state_remote(state)
    else:
        print(f"  -> slot #{index + 1} is flagged Manual and still overdue - "
              f"no matching post on the feed yet, leaving it pending.")


def check_once():
    """
    One-shot version of run_forever()'s loop body, meant to be invoked by
    an external scheduler (GitHub Actions cron, e.g. every ~30 minutes)
    instead of running as a resident process. State lives in Supabase
    (app_state, key "scheduler_state") rather than a local JSON file,
    since GitHub Actions runners don't persist a filesystem between runs.
    All "today"/"now" here means IST, regardless of the server's own
    clock.

    Every Supabase-dependent step in this function is individually
    wrapped so that a failure in ONE of them (a bad override, a
    transient network blip, a missing table) degrades to "skip that
    step and continue" instead of crashing the whole run - the actual
    posting check always still gets a chance to run. That's the whole
    point of this being defensive: a single bad day of Supabase weather
    should never be able to silently stop posting for the rest of the
    day.

    Fires AT MOST ONE post per invocation - the single earliest overdue,
    not-yet-posted slot that also clears MIN_GAP_MINUTES since the last
    post - then exits. Safe to call as often as you like; it's a no-op
    if nothing is currently due.

    If content_pregen.py already built this slot's carousel earlier,
    that pre-built content is published as-is. Otherwise this builds a
    fresh carousel on the spot, exactly as before that feature existed.

    Slots you've flagged Manual in the PWA (see slot_overrides table)
    are never auto-posted here. Instead, for each overdue manual slot,
    this checks whether a matching post already appeared on the real
    Instagram feed and marks it done if so.
    """
    try:
        state = _load_state_remote()
        state = _ensure_today_schedule_remote(state)
    except Exception as e:
        print(f"[{now_ist().isoformat()}] Could not load or initialize today's schedule "
              f"from Supabase - aborting this run cleanly, will retry next check: {e}")
        traceback.print_exc()
        return

    try:
        override = get_schedule_override(state["date"])
    except Exception as e:
        override = None
        print(f"[{now_ist().isoformat()}] Failed to fetch schedule override, "
              f"continuing without it: {e}")

    if override:
        try:
            _apply_schedule_override(state, override)
        except Exception as e:
            print(f"[{now_ist().isoformat()}] Failed to apply schedule override, "
                  f"continuing with the existing schedule unchanged: {e}")
            traceback.print_exc()

    now = now_ist()

    try:
        manual_indices = get_manual_slot_indices(state["date"])
    except Exception as e:
        # get_manual_slot_indices already fails safe internally (returns
        # an empty set on error) - this is just a second layer of
        # defense in case that ever changes.
        manual_indices = set()
        print(f"[{now.isoformat()}] Failed to fetch manual slot indices, "
              f"treating as none: {e}")

    due_index = None
    for i, iso_time in enumerate(state["planned_times"]):
        if state["posted"][i]:
            continue
        if now < datetime.fromisoformat(iso_time):
            continue
        if i in manual_indices:
            try:
                _check_manual_slot(state, i)
            except Exception as e:
                print(f"[{now.isoformat()}] Failed checking manual slot #{i + 1} "
                      f"against the live feed, will retry next check: {e}")
            continue  # never auto-post a manual slot - just check if you posted it yourself
        due_index = i
        break

    if due_index is None:
        print(f"[{now.isoformat()}] Nothing due for auto-posting right now. "
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

    try:
        _save_state_remote(state)
    except Exception as e:
        print(f"[{now_ist().isoformat()}] Posted successfully but failed to save updated "
              f"state to Supabase - the next run may re-attempt this slot. Manually verify "
              f"the post went out before assuming a duplicate is coming: {e}")
        traceback.print_exc()


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
            # for the next poll instead of firing early.

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
