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

MIN_POSTS_PER_DAY = 9
MAX_POSTS_PER_DAY = 13

# The PWA can override today's post count via the schedule_overrides table
# (anon-writable, like slot_overrides/push_subscriptions - see
# supabase_app_additions.sql). This is a hard server-side ceiling applied
# to whatever it requests, so a bad value written there can't blow up the
# day's posting volume.
MAX_ALLOWED_POSTS_PER_DAY = 20

# Each "post" now bundles this many distinct stories into ONE combined
# carousel (see hourly_run.run_combined) instead of one story per post -
# so MIN/MAX_POSTS_PER_DAY x STORIES_PER_POST is the real daily story
# throughput (e.g. 13-23 posts x 5 stories = 65-115 stories/day).
STORIES_PER_POST = 5
IMAGES_PER_STORY = 2

# Minimum gap enforced between any two consecutive posts AT SCHEDULE-
# GENERATION TIME - this is what keeps a normal day's planned timestamps
# spread out and non-spammy. It is NOT used to delay a post that is
# already due; see CATCHUP_GAP_SECONDS below for that.
MIN_GAP_MINUTES = 25

# Once a slot's planned timestamp has passed, it is due and gets posted
# on the very next tick - no waiting on MIN_GAP_MINUTES. The only gap
# still enforced at that point is this much smaller one, purely to avoid
# firing two posts in the exact same instant when several slots are
# overdue at once (e.g. after downtime) and get caught by the same
# check_once() run.
CATCHUP_GAP_SECONDS = 150  # 2.5 minutes

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


def _extra_slots_for_today(day: date, existing_times: list, count: int,
                            min_gap_minutes: int = MIN_GAP_MINUTES) -> list:
    """
    Draws `count` additional peak-window-weighted times for `day`, each at
    least min_gap_minutes from every time in existing_times AND from the
    current moment (never schedules a new slot in the past). Used when the
    app asks for more posts today than are currently planned.
    """
    now = now_ist()
    weights = [w[4] for w in PEAK_WINDOWS]
    taken = list(existing_times)
    added = []
    max_tries = max(count * 300, 300)
    tries = 0
    while len(added) < count and tries < max_tries:
        tries += 1
        window = random.choices(PEAK_WINDOWS, weights=weights, k=1)[0]
        candidate = _random_time_in_window(day, window)
        if candidate <= now:
            continue
        if all(abs((candidate - t).total_seconds()) >= min_gap_minutes * 60 for t in taken):
            taken.append(candidate)
            added.append(candidate)
    added.sort()
    return added


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


def _resort_state_by_time(state: dict):
    """
    Re-sorts every parallel array in `state` (planned_times/posted/
    notified/failed) into ascending time order and reassigns indices to
    match that order.

    Without this, growing the schedule (see _apply_schedule_overrides)
    would simply append the new times to the end of the lists - even
    though a freshly-drawn time can easily land *earlier* than an
    already-pending slot (they're both just random draws inside the same
    PEAK_WINDOWS). Since the PWA displays/sorts slots by their `index`,
    those newly-added-but-earlier posts would show up at the bottom of
    the list instead of where they actually belong in the day's running
    order. Sorting here keeps index == chronological order, which is the
    invariant the rest of the pipeline (and the app) assumes.

    Safe to call any time state's lists change shape; it's a no-op if
    everything is already in time order.
    """
    rows = list(zip(
        state["planned_times"], state["posted"], state["notified"], state["failed"],
    ))
    rows.sort(key=lambda r: r[0])  # ISO 8601 strings sort chronologically as strings
    state["planned_times"] = [r[0] for r in rows]
    state["posted"] = [r[1] for r in rows]
    state["notified"] = [r[2] for r in rows]
    state["failed"] = [r[3] for r in rows]


def _resync_daily_slots_skeleton(state: dict):
    """
    Rewrites app_state[SLOTS_KEY] to match state['planned_times'] after an
    override adds/removes/moves slots, WITHOUT losing any content that
    content_pregen.py already built for a slot that still exists.

    Matches prior slots to new slots by their planned_time value (not by
    index) - state's arrays may have just been re-sorted by
    _resort_state_by_time, so a slot's position can shift even when its
    own timestamp hasn't changed. Matching on the timestamp itself keeps
    already-built content attached to the correct time instead of
    silently jumping to whatever slot now happens to sit at the old
    index. Slots with a changed/removed timestamp simply get a fresh
    empty skeleton entry, same as _publish_schedule_skeleton does at
    midnight.
    """
    today_str = state["date"]
    slots_state = get_state(SLOTS_KEY, default={})
    existing_by_time = {}
    if slots_state.get("date") == today_str:
        existing_by_time = {s["planned_time"]: s for s in slots_state.get("slots", [])}

    new_slots = []
    for i, iso_time in enumerate(state["planned_times"]):
        prior = existing_by_time.get(iso_time)
        if prior and prior.get("image_urls"):
            prior["index"] = i  # position may have shifted after a resort - refresh it
            new_slots.append(prior)
        else:
            new_slots.append({"index": i, "planned_time": iso_time, "image_urls": [], "caption": "", "stories": []})

    save_state(SLOTS_KEY, {"date": today_str, "slots": new_slots})


def _apply_schedule_overrides(state: dict) -> dict:
    """
    Applies whatever the PWA has requested for today via the
    schedule_overrides table (posts-per-day count, and/or specific times
    for individual not-yet-posted slots). Safe to call every tick: it's a
    no-op once state already matches the request. Already-posted slots are
    never touched - editing an index that's already fired is ignored.
    """
    override = get_schedule_override(state["date"])
    if not override:
        return state

    changed = False
    day = today_ist()

    # --- per-slot time edits: {"3": "14:30", ...} maps index -> IST HH:MM ---
    for idx_str, hhmm in (override.get("time_edits") or {}).items():
        try:
            idx = int(idx_str)
            h, m = (int(part) for part in hhmm.split(":"))
        except (ValueError, AttributeError):
            continue
        if idx < 0 or idx >= len(state["planned_times"]) or state["posted"][idx]:
            continue  # can't move a slot that's already posted, or that doesn't exist
        new_iso = datetime(day.year, day.month, day.day, h, m, tzinfo=IST).isoformat()
        if state["planned_times"][idx] != new_iso:
            state["planned_times"][idx] = new_iso
            changed = True

    # --- total count for today: grow or shrink the trailing not-yet-posted slots ---
    target = override.get("target_count")
    if target is not None:
        target = max(1, min(int(target), MAX_ALLOWED_POSTS_PER_DAY))
        posted_count = sum(state["posted"])
        pending_indices = [i for i, posted in enumerate(state["posted"]) if not posted]
        desired_pending = max(0, target - posted_count)

        if desired_pending > len(pending_indices):
            existing_times = [datetime.fromisoformat(t) for t in state["planned_times"]]
            new_times = _extra_slots_for_today(day, existing_times, desired_pending - len(pending_indices))
            for t in new_times:
                state["planned_times"].append(t.isoformat())
                state["posted"].append(False)
                state["notified"].append(False)
                state["failed"].append(False)
            changed = True
        elif desired_pending < len(pending_indices):
            # Trim from the latest-planned pending slots first, so anything
            # sooner (and anything already posted) is left untouched.
            to_remove = sorted(pending_indices, key=lambda i: state["planned_times"][i])[desired_pending:]
            for i in sorted(to_remove, reverse=True):
                del state["planned_times"][i]
                del state["posted"][i]
                del state["notified"][i]
                del state["failed"][i]
            changed = True

    if changed:
        _resort_state_by_time(state)  # keep index order == time order (see docstring)
        _save_state_remote(state)
        _resync_daily_slots_skeleton(state)
        print(f"[{now_ist().isoformat()}] Applied schedule override from the app: "
              f"{len(state['planned_times'])} total slot(s) today.")
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
            "failed": [False] * len(planned),
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
        if "failed" not in state:
            state["failed"] = [False] * len(state["planned_times"])
            changed = True
        if changed:
            _save_state_remote(state)
    return _apply_schedule_overrides(state)


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


def _publish_prebuilt_slot(slot: dict) -> bool:
    """Publishes a slot's already-built content (images already uploaded,
    stories already reserved in Supabase by content_pregen.py) instead of
    building anything fresh. Reuses the same 'verify against the real IG
    feed if the API call errors' safety net as hourly_run.run_combined.

    Returns True if the post is confirmed live (API call succeeded, or a
    feed check confirmed it went live despite an API error), False if it
    genuinely did not post."""
    from instagram_publish import (
        post_carousel_to_instagram, post_to_instagram, find_recent_matching_post,
    )
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
        en_ok = True
    except Exception as e:
        print(f"  -> Instagram publish failed: {e}")
        print("  -> checking the account's recent media in case it actually posted "
              "despite the error...")
        media_id = find_recent_matching_post(caption, account="en")
        if media_id:
            print(f"  -> confirmed: post {media_id} actually went live despite the error.")
            en_ok = True
        else:
            print("  -> confirmed: it genuinely did not post. The stories in this slot "
                  "were already reserved at pre-generation time, so they won't be "
                  "retried automatically - check the app/logs and post manually if needed.")
            en_ok = False

    # Hindi sister-page content, pre-built alongside English by
    # content_pregen.py (see run_combined's POST_HINDI path). Published
    # independently - a Hindi failure here never flips en_ok back to
    # False, since the English post's outcome is already decided above.
    image_urls_hi = slot.get("image_urls_hi") or []
    caption_hi = slot.get("caption_hi") or ""
    if image_urls_hi:
        print(f"  -> [hi] publishing pre-built Hindi content ({len(image_urls_hi)} images)...")
        try:
            if len(image_urls_hi) >= 2:
                media_id_hi = post_carousel_to_instagram(image_urls_hi, caption_hi, account="hi")
            else:
                media_id_hi = post_to_instagram(image_urls_hi[0], caption_hi, account="hi")
            print(f"  -> [hi] posted. Media ID: {media_id_hi}")
        except Exception as e:
            print(f"  -> [hi] Instagram publish failed: {e}")
            media_id_hi = find_recent_matching_post(caption_hi, account="hi")
            if media_id_hi:
                print(f"  -> [hi] confirmed: post {media_id_hi} actually went live despite the error.")
            else:
                print("  -> [hi] confirmed: it genuinely did not post this slot - "
                      "only affects the Hindi page, English is unaffected.")

    return en_ok


def _push_slot_status_remote(index: int, success: bool):
    """Writes the real posted/failed outcome onto the app-visible
    daily_slots state so the companion PWA shows a genuine failure
    instead of a clock-based 'done' guess."""
    slots_state = get_state(SLOTS_KEY, default={})
    if slots_state.get("date") != today_ist().isoformat():
        return
    for slot in slots_state.get("slots", []):
        if slot.get("index") == index:
            slot["posted"] = success
            slot["failed"] = not success
    save_state(SLOTS_KEY, slots_state)


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

    Fires EVERY overdue, not-yet-posted, non-Manual slot found in one
    pass, in timestamp order - a due post is the first priority and is
    never held back to preserve spacing. The only gap still enforced
    between posts is CATCHUP_GAP_SECONDS (2.5 min), just so a pile of
    simultaneously-overdue slots (e.g. after downtime) don't fire in the
    same instant; it never delays a single overdue post waiting on its
    own. Safe to call as often as you like; it's a no-op if nothing is
    currently due.

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

    # Collect every overdue, not-yet-posted slot up front, in timestamp
    # order. Manual slots are checked against the real feed inline (as
    # before) and never enter this list.
    due_indices = []
    for i, iso_time in enumerate(state["planned_times"]):
        if state["posted"][i]:
            continue
        if now < datetime.fromisoformat(iso_time):
            continue
        if i in manual_indices:
            _check_manual_slot(state, i)
            continue  # never auto-post a manual slot - just check if you posted it yourself
        due_indices.append(i)

    if not due_indices:
        print(f"[{now.isoformat()}] Nothing due for auto-posting right now. "
              f"{sum(state['posted'])}/{len(state['posted'])} posted today.")
        return

    print(f"[{now.isoformat()}] {len(due_indices)} slot(s) overdue - "
          f"posting all of them now, in order.")

    if "failed" not in state:
        state["failed"] = [False] * len(state["planned_times"])

    for due_index in due_indices:
        # CATCHUP_GAP_SECONDS only protects against firing two posts in
        # the same instant when a batch of slots is overdue together; it
        # never holds a post back to preserve normal-day spacing (that's
        # MIN_GAP_MINUTES's job, applied only when the schedule is first
        # generated). If the gap hasn't cleared yet, just wait a beat and
        # re-check rather than skipping the slot to a later run.
        last_post_iso = state.get("last_post_time")
        if last_post_iso:
            elapsed = (now_ist() - datetime.fromisoformat(last_post_iso)).total_seconds()
            if elapsed < CATCHUP_GAP_SECONDS:
                time.sleep(CATCHUP_GAP_SECONDS - elapsed)

        planned_dt = datetime.fromisoformat(state["planned_times"][due_index])
        print(f"[{now_ist().isoformat()}] Firing scheduled post "
              f"#{due_index + 1}/{len(state['planned_times'])} "
              f"(planned {planned_dt.strftime('%H:%M')} IST)")
        success = False
        try:
            prebuilt = _get_prebuilt_slot(due_index)
            if prebuilt:
                success = _publish_prebuilt_slot(prebuilt)
            else:
                print("  -> no pre-built content found for this slot - building fresh now.")
                hourly_run.run_combined(
                    story_count=STORIES_PER_POST,
                    images_per_story=IMAGES_PER_STORY,
                    apply_jitter=False,
                )
                success = True
        except Exception:
            print("Post attempt failed - will not retry this slot, "
                  "continuing with the rest of today's schedule.")
            traceback.print_exc()
            success = False

        # Defensive pad: state["posted"]/["failed"] should always be kept in
        # sync with state["planned_times"] by _apply_schedule_overrides(),
        # but if they're ever short for any reason, extend rather than
        # IndexError here - a crash on this line, after a real Instagram
        # publish already succeeded, is exactly what caused slots to get
        # re-posted on the next tick (the "posted" flag never made it to
        # Supabase because the crash happened before _save_state_remote).
        while len(state["posted"]) <= due_index:
            state["posted"].append(False)
        while len(state["failed"]) <= due_index:
            state["failed"].append(False)

        try:
            state["posted"][due_index] = True
            state["failed"][due_index] = not success
            if success:
                state["last_post_time"] = now_ist().isoformat()
        finally:
            # Always persist, even if something above this line misbehaves -
            # a real publish must never be lost from state.
            _save_state_remote(state)  # save after each slot - a mid-run interruption loses nothing
        _push_slot_status_remote(due_index, success)


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

        # Fires the single earliest overdue, not-yet-posted slot per pass
        # - the 30s poll loop means the NEXT overdue slot (if any) gets
        # picked up moments later anyway, so this never sits on a backlog.
        # The only gap enforced here is CATCHUP_GAP_SECONDS, purely so two
        # simultaneously-overdue slots don't fire in the very same
        # instant - it never holds a single overdue post back waiting on
        # MIN_GAP_MINUTES (that constant only shapes spacing when a day's
        # schedule is first generated, not when a post is already due).
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
            gap_ok = last_post_dt is None or (now - last_post_dt).total_seconds() >= CATCHUP_GAP_SECONDS

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
