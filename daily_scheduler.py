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
from supabase_client import get_state, save_state, get_manual_slot_indices, get_schedule_override

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
# Raised from 5 to 20 per request. At STORIES_PER_POST=5 this means up to
# 20 * 5 = 100 stories/day (vs. 25/day before) - if that throughput ends up
# too aggressive for the account, the more surgical knob to bring back down
# is STORIES_PER_POST below, not this one.
MAX_POSTS_PER_DAY = 20

# Each "post" now bundles this many distinct stories into ONE combined
# carousel (see hourly_run.run_combined) instead of one story per post -
# so MIN/MAX_POSTS_PER_DAY x STORIES_PER_POST is the real daily story
# throughput (e.g. 3-20 posts x 5 stories = 15-100 stories/day).
STORIES_PER_POST = 5
IMAGES_PER_STORY = 2

# Minimum gap enforced between any two consecutive posts, so a busy
# random draw can't accidentally cluster several posts back-to-back
# (which would read as spammy no matter how the times were chosen).
#
# NOTE: with MAX_POSTS_PER_DAY now up to 20, double check this still
# makes sense - 20 posts x 25 min minimum gap = 500 minutes (~8.3 hours)
# of forced minimum spacing alone, which comfortably fits inside the
# PEAK_WINDOWS below (roughly 7:00-23:45 IST, ~16.75 hours), but leaves
# less slack than before for the random draw to actually spread posts
# out further than the minimum. If posts start feeling bunched at the
# 20-post end of the range, raise this or narrow MAX_POSTS_PER_DAY back
# down rather than shrinking PEAK_WINDOWS.
MIN_GAP_MINUTES = 25

MAX_TARGET_POSTS = 25  # hard ceiling on the PWA's "posts today (total)" override

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
    (7, 0, 9, 30, 3),    # morning commute / breakfast scrolling
    (12, 0, 14, 0, 2),   # lunch break
    (17, 0, 19, 0, 3),   # evening commute
    (19, 0, 22, 30, 5),  # prime time - dinner through late evening, highest weight
    (22, 30, 23, 45, 1), # late-night scrollers, light coverage
]


def _random_time_in_window(day: date, window) -> datetime:
    sh, sm, eh, em, _ = window
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, eh, em, tzinfo=IST)
    delta_seconds = int((end - start).total_seconds())
    return start + timedelta(seconds=random.randint(0, max(delta_seconds, 0)))


def _generate_additional_times(existing_times: list, count: int, day: date,
                                not_before: datetime = None,
                                min_gap_minutes: int = MIN_GAP_MINUTES) -> list:
    """
    Draws up to `count` additional weighted-random times for `day`
    (same PEAK_WINDOWS logic as generate_daily_schedule), each at least
    min_gap_minutes from every time in `existing_times` AND from every
    other newly-drawn time. If `not_before` is given, only candidates
    after that instant are accepted - used when growing TODAY's already
    -partially-elapsed schedule, so a new slot can't land in the past or
    a couple minutes from now.

    May return fewer than `count` times if the day's remaining windows
    can't fit that many more without violating the gap - that's fine,
    the caller schedules as many as it found room for and logs the rest
    as "couldn't fit today."
    """
    weights = [w[4] for w in PEAK_WINDOWS]
    all_times = list(existing_times)
    new_times = []
    max_tries_total = max(count, 1) * 300
    tries = 0
    while len(new_times) < count and tries < max_tries_total:
        tries += 1
        window = random.choices(PEAK_WINDOWS, weights=weights, k=1)[0]
        candidate = _random_time_in_window(day, window)
        if not_before is not None and candidate <= not_before:
            continue
        if all(abs((candidate - t).total_seconds()) >= min_gap_minutes * 60 for t in all_times):
            all_times.append(candidate)
            new_times.append(candidate)
    new_times.sort()
    return new_times


def _append_slots_to_skeleton(date_str: str, new_times: list, start_index: int):
    """Adds skeleton (unbuilt) rows for newly-added slots to app_state[SLOTS_KEY]
    so the PWA shows them immediately, matching _publish_schedule_skeleton's format."""
    slots_state = get_state(SLOTS_KEY, default={})
    if slots_state.get("date") != date_str:
        return  # today's skeleton doesn't exist yet somehow - nothing to append to
    for offset, t in enumerate(new_times):
        slots_state["slots"].append({
            "index": start_index + offset, "planned_time": t.isoformat(),
            "image_urls": [], "caption": "", "stories": [],
        })
    save_state(SLOTS_KEY, slots_state)


def _remove_slots_from_skeleton(date_str: str, removed_indices: list):
    """Drops removed slots' rows from app_state[SLOTS_KEY] so the PWA stops showing them."""
    slots_state = get_state(SLOTS_KEY, default={})
    if slots_state.get("date") != date_str:
        return
    removed = set(removed_indices)
    slots_state["slots"] = [s for s in slots_state.get("slots", []) if s.get("index") not in removed]
    save_state(SLOTS_KEY, slots_state)


def _apply_target_count(state: dict, override: dict) -> dict:
    """
    Applies the PWA's requested total post count for today (schedule_
    overrides.target_count) by growing or shrinking state["planned_times"]
    (and the mirrored posted[]/notified[] arrays) - never touching already
    -posted slots, and never moving any existing slot's index, so
    time_edits and the Manual flag (both keyed by index) stay valid.

    Growing: new slots are appended at the END (indices current_total,
    current_total+1, ...) with freshly-drawn weighted-random times later
    today, honouring MIN_GAP_MINUTES against everything already planned.

    Shrinking: not-yet-posted slots are removed from the END first (the
    ones with the highest indices), so earlier indices - and anything
    already keyed off them - never shift.

    Hard-capped at MAX_TARGET_POSTS regardless of what the PWA sent, and
    never allowed to drop below however many slots are already posted.
    """
    target = override.get("target_count") if override else None
    if not target:
        return state
    try:
        target = int(target)
    except (TypeError, ValueError):
        print(f"[schedule-override] ignoring malformed target_count {target!r}")
        return state

    posted_count = sum(state["posted"])
    target = max(min(target, MAX_TARGET_POSTS), posted_count)
    current_total = len(state["planned_times"])

    if target == current_total:
        return state

    if target < current_total:
        remove_needed = current_total - target
        removed_indices = []
        for i in range(current_total - 1, -1, -1):
            if remove_needed == 0:
                break
            if not state["posted"][i]:
                removed_indices.append(i)
                remove_needed -= 1
        if not removed_indices:
            return state
        for i in sorted(removed_indices, reverse=True):
            del state["planned_times"][i]
            del state["posted"][i]
            del state["notified"][i]
        print(f"[schedule-override] target_count={target}: removed "
              f"{len(removed_indices)} not-yet-posted slot(s) from the end of "
              f"today's schedule")
        _save_state_remote(state)
        _remove_slots_from_skeleton(state["date"], removed_indices)

    else:  # target > current_total
        add_needed = target - current_total
        now = now_ist()
        existing_dts = [datetime.fromisoformat(t) for t in state["planned_times"]]
        new_times = _generate_additional_times(
            existing_dts, add_needed, today_ist(), not_before=now,
        )
        if not new_times:
            print(f"[schedule-override] target_count={target}: couldn't fit any "
                  f"additional slot(s) later today - not enough room left before "
                  f"midnight without violating the minimum gap.")
            return state
        if len(new_times) < add_needed:
            print(f"[schedule-override] target_count={target}: could only fit "
                  f"{len(new_times)}/{add_needed} additional slot(s) later today - "
                  f"scheduling {current_total + len(new_times)} for today instead.")
        for t in new_times:
            state["planned_times"].append(t.isoformat())
            state["posted"].append(False)
            state["notified"].append(False)
        print(f"[schedule-override] target_count={target}: added {len(new_times)} "
              f"new slot(s) at "
              f"{', '.join(t.strftime('%H:%M') for t in new_times)}")
        _save_state_remote(state)
        _append_slots_to_skeleton(state["date"], new_times, start_index=current_total)

    return state


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


def _publish_schedule_skeleton(today_str: str, planned: list):
    """
    Pushes JUST the time slots (no content yet) to app_state[SLOTS_KEY] the
    moment today's schedule is decided, so the companion PWA can show the
    whole day's timestamps immediately at midnight. Each slot's actual
    carousel (image_urls/caption/stories) gets filled in later, ~30 min
    before that slot's own post time - see _build_due_content_remote().
    """
    save_state(SLOTS_KEY, {
        "date": today_str,
        "slots": [
            {"index": i, "planned_time": t.isoformat(), "image_urls": [], "caption": "", "stories": []}
            for i, t in enumerate(planned)
        ],
    })


def _apply_schedule_override(state: dict) -> dict:
    """
    Applies any pending PWA-requested schedule changes (schedule_overrides
    row, saved by saveScheduleChanges() in docs/app.js) to today's state:
    time_edits (move a not-yet-posted slot's time) and target_count (add
    or remove slots for the day, capped at MAX_TARGET_POSTS). Called every
    tick from _ensure_today_schedule_remote(), so both content_pregen.py
    (build timing) and daily_scheduler.py --check-once (fire timing) see
    the same edited schedule - previously this table was written by the
    PWA but never read back by anything, so edits silently had no effect.

    Edits to already-posted or out-of-range slot indices are ignored.
    Existing slots' indices never change (only removed/appended at the
    end), so posted[], notified[], time_edits, and Manual flags - all
    keyed by index - stay valid across an edit.
    """
    override = get_schedule_override(state["date"])
    if not override:
        return state

    state = _apply_target_count(state, override)

    time_edits = override.get("time_edits") or {}
    if not time_edits:
        return state

    changed_indices = {}
    for idx_str, hhmm in time_edits.items():
        try:
            idx = int(idx_str)
            hh, mm = (int(p) for p in str(hhmm).split(":"))
        except (ValueError, TypeError):
            print(f"[schedule-override] skipping malformed time edit {idx_str}={hhmm!r}")
            continue
        if idx < 0 or idx >= len(state["planned_times"]):
            continue
        if state["posted"][idx]:
            continue  # can't move a slot that's already fired
        day = today_ist()
        new_dt = datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)
        if state["planned_times"][idx] != new_dt.isoformat():
            state["planned_times"][idx] = new_dt.isoformat()
            changed_indices[idx] = new_dt

    if changed_indices:
        summary = ", ".join(f"#{i + 1} -> {t.strftime('%H:%M')}" for i, t in changed_indices.items())
        print(f"[schedule-override] applying {len(changed_indices)} PWA time edit(s): {summary}")
        _save_state_remote(state)
        # Keep the PWA's own display (app_state[SLOTS_KEY]) in sync too -
        # otherwise the schedule would fire at the new time but still show
        # the old time in the app.
        slots_state = get_state(SLOTS_KEY, default={})
        if slots_state.get("date") == state["date"]:
            for slot in slots_state.get("slots", []):
                if slot.get("index") in changed_indices:
                    slot["planned_time"] = changed_indices[slot["index"]].isoformat()
            save_state(SLOTS_KEY, slots_state)

    return state


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

    state = _apply_schedule_override(state)
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
    from token_refresh import ensure_token_fresh

    ensure_token_fresh(account="en")
    if slot.get("image_urls_hi"):
        ensure_token_fresh(account="hi")

    image_urls = slot["image_urls"]
    caption = slot["caption"]
    print(f"  -> publishing pre-built content ({len(slot.get('stories', []))} stories, "
          f"{len(image_urls)} images)...")
    try:
        if len(image_urls) >= 2:
            media_id = post_carousel_to_instagram(image_urls, caption, account="en")
        else:
            media_id = post_to_instagram(image_urls[0], caption, account="en")
        print(f"  -> posted. Media ID: {media_id}")
    except Exception as e:
        print(f"  -> Instagram publish failed: {e}")
        print("  -> checking the account's recent media in case it actually posted "
              "despite the error...")
        media_id = find_recent_matching_post(caption, account="en")
        if media_id:
            print(f"  -> confirmed: post {media_id} actually went live despite the error.")
        else:
            print("  -> confirmed: it genuinely did not post. The stories in this slot "
                  "were already reserved at pre-generation time, so they won't be "
                  "retried automatically - check the app/logs and post manually if needed.")

    # --- Publish the Hindi sister post, if content_pregen.py built one for
    # this slot. English is never blocked or rolled back by a Hindi
    # failure - the English post above already happened either way.
    image_urls_hi = slot.get("image_urls_hi") or []
    caption_hi = slot.get("caption_hi") or ""
    if image_urls_hi:
        print(f"  -> publishing pre-built Hindi content ({len(image_urls_hi)} images)...")
        try:
            if len(image_urls_hi) >= 2:
                media_id_hi = post_carousel_to_instagram(image_urls_hi, caption_hi, account="hi")
            else:
                media_id_hi = post_to_instagram(image_urls_hi[0], caption_hi, account="hi")
            print(f"  -> [hi] posted. Media ID: {media_id_hi}")
        except Exception as e:
            print(f"  -> [hi] Instagram publish failed: {e}")
            print("  -> [hi] checking the Hindi account's recent media in case it actually "
                  "posted despite the error...")
            media_id_hi = find_recent_matching_post(caption_hi, account="hi")
            if media_id_hi:
                print(f"  -> [hi] confirmed: post {media_id_hi} actually went live despite the error.")
            else:
                print("  -> [hi] confirmed: it genuinely did not post this slot - the English "
                      "post already went out fine, this only affects the Hindi page.")


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
    (the companion app's rolling "content ready ~30 min ahead" feature),
    that pre-built content is published as-is. Otherwise this builds a
    fresh carousel on the spot, exactly as before that feature existed.

    Slots you've flagged Manual in the PWA (see slot_overrides table)
    are never auto-posted here. Instead, for each overdue manual slot,
    this checks whether a matching post already appeared on the real
    Instagram feed (i.e. you posted it yourself) and marks it done if
    so - so the app's progress reflects reality and nothing gets
    double-posted later by mistake.
    """
    state = _load_state_remote()
    state = _ensure_today_schedule_remote(state)

    now = now_ist()
    manual_indices = get_manual_slot_indices(state["date"])

    due_index = None
    for i, iso_time in enumerate(state["planned_times"]):
        if state["posted"][i]:
            continue
        if now < datetime.fromisoformat(iso_time):
            continue
        if i in manual_indices:
            _check_manual_slot(state, i)
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
