"""
daily_scheduler.py

Long-running process that posts somewhere between MIN_POSTS_PER_DAY and
MAX_POSTS_PER_DAY times a day, at random times biased toward the hours
when Instagram audiences are generally most active - never on a fixed
:00/:30 grid, and never bunched together (a minimum gap is enforced
between consecutive posts).

Each individual post still goes through hourly_run.run_combined() as the
fallback content builder, or (normally) publishes whatever
content_pregen.py already built for that slot. Either way, ONE slot now
carries content for BOTH languages (image_urls/caption for English,
image_urls_hi/caption_hi for Hindi - see content_pregen.py /
hourly_run.run_combined), but each language gets its OWN clock time and
its OWN posted-flag, so English and Hindi can post at different times
(or not at all, if flagged Manual) without needing two separate slot
tracks. This is the fix for the bug where Hindi silently never got
published: _publish_prebuilt_slot used to only ever touch the English
fields.

The companion PWA can request changes to today's schedule (total post
count, and/or specific times for individual not-yet-posted slots, now
per language) - see schedule_overrides / slot_overrides in
supabase_app_additions.sql + migration_hi_manual_flag.sql.
check_once() fetches and applies any pending override at the start of
every run, before checking what's due.

Run this as your one long-lived process instead of an hourly cron job:
    python daily_scheduler.py

State (today's planned times for both languages + which have fired) is
persisted to scheduler_state.json next to this file (local mode) or to
Supabase app_state under STATE_KEY (check-once / GitHub Actions mode),
so a restart mid-day resumes correctly instead of re-planning (and
potentially over-posting) or silently skipping the rest of the day.
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

# NOTE: get_manual_slot_indices is called below with a `lang` kwarg
# ("en" / "hi"), matching the manual_en / manual_hi split added to
# slot_overrides by migration_hi_manual_flag.sql. supabase_client.py's
# get_manual_slot_indices needs to accept that kwarg and select the
# right column - see the note at the bottom of this file if it doesn't
# yet.

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler_state.json")
STATE_KEY = "scheduler_state"  # Supabase app_state key, used by check_once()
SLOTS_KEY = "daily_slots"      # Supabase app_state key holding pre-generated content, written by content_pregen.py

LANGS = ("en", "hi")

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
# throughput (e.g. 13-23 posts x 5 stories = 65-115 stories/day). This is
# per language - the same 5 stories are reused for both the en and hi
# carousels of a given slot, just posted at (usually) different times.
STORIES_PER_POST = 5
IMAGES_PER_STORY = 2

# Minimum gap enforced between any two consecutive posts ON THE SAME
# ACCOUNT, so a busy random draw can't accidentally cluster several
# posts back-to-back (which would read as spammy no matter how the
# times were chosen). en and hi are separate IG accounts, so their gaps
# are tracked and enforced independently of each other.
MIN_GAP_MINUTES = 25

# How often the main loop wakes up to check whether it's time to post.
# Coarse enough to be cheap, fine enough that posts fire within a minute
# of their planned time.
POLL_SECONDS = 30

# Windows (24h local time) when engagement tends to be highest, each with
# a relative weight controlling how much of the day's post budget lands
# there. These are general, widely-cited social engagement patterns, not
# an exact science - the point is "mostly during active hours, never all
# bunched at 3am," not minute-perfect optimization. Used independently
# for both the en and hi time draws.
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


def generate_daily_schedule(day: date, num_posts: int, min_gap_minutes: int = MIN_GAP_MINUTES) -> list:
    """
    Returns a sorted list of `num_posts` IST-aware datetime objects for
    `day`, weighted toward PEAK_WINDOWS, with at least min_gap_minutes
    between consecutive posts. Pure time-drawing helper - doesn't decide
    num_posts itself, so the same function can be reused to draw the en
    and hi tracks independently against the SAME num_posts (content is
    shared 1:1 by slot index between languages, so the two tracks must
    have equal length even though their actual clock times differ).
    """
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


def generate_daily_schedule_dual(day: date = None, min_posts: int = MIN_POSTS_PER_DAY,
                                  max_posts: int = MAX_POSTS_PER_DAY,
                                  min_gap_minutes: int = MIN_GAP_MINUTES) -> tuple:
    """
    Decides ONE slot count for the day (shared, since content_pregen
    builds one story-bundle per slot index that both languages draw
    from), then independently draws en and hi time lists of that same
    length. Returns (times_en, times_hi), each a sorted list of
    `num_posts` IST datetimes - the two lists are NOT aligned/sorted
    against each other, i.e. times_en[i] and times_hi[i] both belong to
    slot i's content but can land at completely different times of day.
    """
    day = day or today_ist()
    num_posts = random.randint(min_posts, max_posts)
    times_en = generate_daily_schedule(day, num_posts, min_gap_minutes)
    times_hi = generate_daily_schedule(day, num_posts, min_gap_minutes)
    return times_en, times_hi


def _weighted_time_in_remaining_day(day: date, not_before: datetime, existing_times: list,
                                     min_gap_minutes: int = MIN_GAP_MINUTES,
                                     max_tries: int = 300):
    """
    Draws one new weighted-random time from PEAK_WINDOWS, restricted to
    the portion of each window still ahead of `not_before`, and
    respecting min_gap_minutes against every time already in
    `existing_times` (pass the SAME-LANGUAGE existing times only - en
    and hi gaps are independent). Returns None if no valid slot could be
    found within max_tries (e.g. the day's remaining peak windows are
    already packed) - the caller treats that as "no more room today."
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


def _load_state_remote() -> dict:
    return get_state(STATE_KEY, default={})


def _save_state_remote(state: dict):
    save_state(STATE_KEY, state)


def _publish_schedule_skeleton(today_str: str, planned_en: list, planned_hi: list):
    """
    Pushes JUST the time slots (no content yet) to app_state[SLOTS_KEY] the
    moment today's schedule is decided, so the companion PWA can show the
    whole day's timestamps for both languages immediately at midnight.
    Each slot's actual carousel content (image_urls/caption/stories and
    their _hi counterparts) gets filled in later, ~30 min before that
    slot's own EARLIEST post time - see content_pregen.py.
    """
    save_state(SLOTS_KEY, {
        "date": today_str,
        "slots": [
            {
                "index": i,
                "planned_time": t_en.isoformat(),
                "planned_time_hi": t_hi.isoformat(),
                "image_urls": [], "caption": "", "stories": [],
                "image_urls_hi": [], "caption_hi": [],
            }
            for i, (t_en, t_hi) in enumerate(zip(planned_en, planned_hi))
        ],
    })


def _sync_slots_skeleton(state: dict):
    """
    Re-syncs app_state[SLOTS_KEY] (the PWA's display) with the current
    planned_times/planned_times_hi after a schedule override changed the
    slot count or times - adds skeleton entries for newly-added slots,
    drops entries for slots that were removed, and updates planned_time /
    planned_time_hi on edited ones. Leaves already-built content
    (image_urls/caption/stories and the _hi fields) on every other slot
    untouched.

    Best-effort: failure here never blocks posting - it just means the
    app's display lags behind until the next successful sync.
    """
    try:
        slots_state = get_state(SLOTS_KEY, default={})
        if slots_state.get("date") != state["date"]:
            _publish_schedule_skeleton(
                state["date"],
                [datetime.fromisoformat(t) for t in state["planned_times"]],
                [datetime.fromisoformat(t) for t in state["planned_times_hi"]],
            )
            return

        by_index = {s["index"]: s for s in slots_state.get("slots", [])}
        new_slots = []
        for i, (iso_en, iso_hi) in enumerate(zip(state["planned_times"], state["planned_times_hi"])):
            existing = by_index.get(i)
            if existing:
                existing["planned_time"] = iso_en
                existing["planned_time_hi"] = iso_hi
                new_slots.append(existing)
            else:
                new_slots.append({
                    "index": i, "planned_time": iso_en, "planned_time_hi": iso_hi,
                    "image_urls": [], "caption": "", "stories": [],
                    "image_urls_hi": [], "caption_hi": "",
                })
        slots_state["slots"] = new_slots
        save_state(SLOTS_KEY, slots_state)
    except Exception as e:
        print(f"[{now_ist().isoformat()}] Schedule override: failed to sync the daily_slots "
              f"skeleton (non-fatal - app display may lag, will retry next check): {e}")


def _apply_target_count(state: dict, target_count) -> bool:
    """
    Grows or shrinks today's schedule to `target_count` slots, in place
    on `state` - applied to BOTH the en and hi tracks together, since
    slot content is shared 1:1 by index between languages and the two
    tracks must stay the same length. Returns True if state actually
    changed.

    - target_count is clamped to [max(posted_count_en, posted_count_hi),
      MAX_TARGET_POSTS] - never drops below however many are already
      posted in EITHER language, and MAX_TARGET_POSTS is a hard
      server-side ceiling regardless of what the PWA sent.
    - Shrinking removes not-yet-posted-in-either-language slots from the
      end only (highest index first), so it never renumbers a slot that
      has already posted (in either language) or has an existing
      time-edit/Manual flag pointing at it.
    - Growing appends new slots at the end with freshly drawn
      weighted-random times for BOTH tracks independently, each
      respecting MIN_GAP_MINUTES against every slot already planned IN
      THAT SAME LANGUAGE, and only within the time remaining today. If
      one language runs out of room before the other, it adds as many
      as will fit for the language that's constrained and logs how many
      it dropped - it won't cram them in or desync the two tracks'
      lengths (a slot that got an en time but no hi time isn't added at
      all, since content_pregen needs both).
    """
    if target_count is None:
        return False

    try:
        target_count = int(target_count)
    except (TypeError, ValueError):
        return False

    posted_floor = max(state["posted"].count(True), state["posted_hi"].count(True))
    current_count = len(state["planned_times"])
    target_count = max(posted_floor, min(target_count, MAX_TARGET_POSTS))

    if target_count == current_count:
        return False

    if target_count < current_count:
        remove_n = current_count - target_count
        removable_indices = [
            i for i in range(current_count - 1, -1, -1)
            if not state["posted"][i] and not state["posted_hi"][i]
        ]
        to_remove = sorted(removable_indices[:remove_n], reverse=True)
        for i in to_remove:
            for key in ("planned_times", "posted", "planned_times_hi", "posted_hi", "notified", "notified_hi"):
                if key in state and i < len(state[key]):
                    del state[key][i]
        print(f"[{now_ist().isoformat()}] Schedule override: removed {len(to_remove)} "
              f"not-yet-posted slot(s), now {len(state['planned_times'])} planned today.")
        return len(to_remove) > 0

    # growing - draw both tracks independently, but only keep as many
    # new slots as BOTH tracks could find room for (equal-length
    # requirement), so we never add a slot with an en time and no hi
    # time or vice versa.
    add_n = target_count - current_count
    day = today_ist()
    now = now_ist()
    existing_en = [datetime.fromisoformat(t) for t in state["planned_times"]]
    existing_hi = [datetime.fromisoformat(t) for t in state["planned_times_hi"]]
    added = 0
    for _ in range(add_n):
        cand_en = _weighted_time_in_remaining_day(day, now, existing_en)
        cand_hi = _weighted_time_in_remaining_day(day, now, existing_hi)
        if cand_en is None or cand_hi is None:
            break
        existing_en.append(cand_en)
        existing_hi.append(cand_hi)
        state["planned_times"].append(cand_en.isoformat())
        state["planned_times_hi"].append(cand_hi.isoformat())
        state["posted"].append(False)
        state["posted_hi"].append(False)
        if "notified" in state:
            state["notified"].append(False)
        if "notified_hi" in state:
            state["notified_hi"].append(False)
        added += 1

    dropped = add_n - added
    print(f"[{now_ist().isoformat()}] Schedule override: added {added} new slot(s) "
          f"(requested {add_n})"
          f"{f' - could not fit {dropped} more before midnight (en and/or hi ran out of room) without breaking the gap rule' if dropped else ''}. "
          f"Now {len(state['planned_times'])} planned today.")
    return added > 0


def _apply_time_edits(state: dict, time_edits: dict) -> bool:
    """
    Applies specific time changes to not-yet-posted slots, independently
    per language. `time_edits` is keyed by slot index (string, since it
    comes from JSON). Each value is EITHER:
      - a dict like {"en": "19:30"} / {"hi": "21:00"} / {"en": ..., "hi": ...}
        (new, preferred shape - lets the PWA edit one or both languages), OR
      - a plain "HH:MM" string (old shape, from before the Hindi fix -
        treated as an English-only edit, so anything already sitting in
        Supabase from before this change keeps working).
    A slot already posted in a given language is left untouched for that
    language even if an edit is present for it (the other language can
    still be edited if it hasn't posted yet). Returns True if anything
    actually changed.
    """
    if not time_edits:
        return False

    day = today_ist()
    changed = False

    def _parse_hhmm(hhmm):
        hour_str, minute_str = str(hhmm).split(":")
        return datetime(day.year, day.month, day.day, int(hour_str), int(minute_str), tzinfo=IST)

    for key, edit in time_edits.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(state["planned_times"]):
            continue

        if isinstance(edit, dict):
            per_lang = edit
        else:
            per_lang = {"en": edit}  # back-compat with the pre-Hindi-fix flat format

        for lang, hhmm in per_lang.items():
            if lang not in LANGS:
                continue
            times_key = "planned_times" if lang == "en" else "planned_times_hi"
            posted_key = "posted" if lang == "en" else "posted_hi"
            if state[posted_key][index]:
                continue
            try:
                new_time = _parse_hhmm(hhmm)
            except (ValueError, TypeError):
                print(f"[{now_ist().isoformat()}] Schedule override: skipping unparseable "
                      f"{lang} time edit for slot #{index + 1}: {hhmm!r}")
                continue
            if datetime.fromisoformat(state[times_key][index]) != new_time:
                state[times_key][index] = new_time.isoformat()
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
        planned_en, planned_hi = generate_daily_schedule_dual(today_ist())
        state = {
            "date": today_str,
            "planned_times": [t.isoformat() for t in planned_en],
            "posted": [False] * len(planned_en),
            "planned_times_hi": [t.isoformat() for t in planned_hi],
            "posted_hi": [False] * len(planned_hi),
            "notified": [False] * len(planned_en),
            "notified_hi": [False] * len(planned_hi),
            "schedule_announced": False,  # flips True once send_notifications.py has pushed the "schedule's ready" alert
        }
        _save_state_remote(state)
        _publish_schedule_skeleton(today_str, planned_en, planned_hi)
        print(f"[{now_ist().isoformat()}] New schedule for {today_str} (IST): "
              f"{len(planned_en)} slots. en at "
              f"{', '.join(t.strftime('%H:%M') for t in planned_en)} | hi at "
              f"{', '.join(t.strftime('%H:%M') for t in planned_hi)}")
    else:
        # Migrating state that predates this fix: an existing today's
        # state won't have the _hi fields yet. Backfill them with a
        # fresh hi draw of the same length so today doesn't lose Hindi
        # posting just because the day started before this deploy.
        changed = False
        if "planned_times_hi" not in state:
            existing_en = [datetime.fromisoformat(t) for t in state["planned_times"]]
            now = now_ist()
            hi_times = []
            for _ in existing_en:
                cand = _weighted_time_in_remaining_day(today_ist(), now, hi_times)
                hi_times.append(cand if cand is not None else now + timedelta(minutes=len(hi_times) * MIN_GAP_MINUTES + 5))
            state["planned_times_hi"] = [t.isoformat() for t in hi_times]
            state["posted_hi"] = [False] * len(existing_en)
            state["notified_hi"] = [False] * len(existing_en)
            changed = True
            print(f"[{now_ist().isoformat()}] Backfilled missing Hindi time track for today's "
                  f"already-in-progress schedule ({len(existing_en)} slots).")
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
    it exists. Returns the slot dict, or None if pregeneration hasn't
    produced this slot yet - in which case check_once() falls back to
    building fresh, exactly like before this feature existed. A slot
    only counts as "prebuilt" for a given language if that language's
    image list is non-empty, since content_pregen.py can in principle
    finish one language before the other.
    """
    slots_state = get_state(SLOTS_KEY, default={})
    if slots_state.get("date") != today_ist().isoformat():
        return None
    for slot in slots_state.get("slots", []):
        if slot.get("index") == due_index and (slot.get("image_urls") or slot.get("image_urls_hi")):
            return slot
    return None


def _publish_prebuilt_slot(slot: dict, lang: str):
    """
    Publishes a slot's already-built content for ONE language (images
    already uploaded, stories already reserved in Supabase by
    content_pregen.py) instead of building anything fresh. Reuses the
    same 'verify against the real IG feed if the API call errors' safety
    net as hourly_run.run_combined, checked against the correct
    language's account.

    lang="en" reads image_urls/caption and posts with account="en".
    lang="hi" reads image_urls_hi/caption_hi and posts with account="hi" -
    this is the code path that was missing entirely before this fix.
    """
    from instagram_publish import (
        post_carousel_to_instagram, post_to_instagram, find_recent_matching_post,
    )

    image_urls = slot["image_urls_hi"] if lang == "hi" else slot["image_urls"]
    caption = slot["caption_hi"] if lang == "hi" else slot["caption"]

    if not image_urls or not caption:
        print(f"  -> slot has no {lang} content yet (content_pregen.py may still be "
              f"working on it, or this language failed to build) - skipping this run, "
              f"will retry next check.")
        return

    print(f"  -> publishing pre-built {lang} content ({len(slot.get('stories', []))} stories, "
          f"{len(image_urls)} images)...")
    try:
        if len(image_urls) >= 2:
            media_id = post_carousel_to_instagram(image_urls, caption, account=lang)
        else:
            media_id = post_to_instagram(image_urls[0], caption, account=lang)
        print(f"  -> posted ({lang}). Media ID: {media_id}")
    except Exception as e:
        print(f"  -> Instagram publish failed ({lang}): {e}")
        print(f"  -> checking the {lang} account's recent media in case it actually posted "
              f"despite the error...")
        media_id = find_recent_matching_post(caption, account=lang)
        if media_id:
            print(f"  -> confirmed: {lang} post {media_id} actually went live despite the error.")
        else:
            print(f"  -> confirmed: the {lang} post genuinely did not go out. The stories in "
                  f"this slot were already reserved at pre-generation time, so they won't be "
                  f"retried automatically - check the app/logs and post manually if needed.")


def _check_manual_slot(state: dict, index: int, lang: str):
    """A slot you've flagged Manual (for this language) is overdue - see
    if it already went live on Instagram (you posted it yourself) and,
    if so, mark it posted so the scheduler and the app both stop
    treating it as pending for that language. If nothing matching is
    found yet, leaves it alone; it'll be checked again on the next
    tick."""
    from instagram_publish import find_recent_matching_post

    slot = _get_prebuilt_slot(index)
    caption = (slot or {}).get("caption_hi" if lang == "hi" else "caption")
    if not slot or not caption:
        return  # content for this slot/language hasn't been built yet - nothing to match against

    media_id = find_recent_matching_post(caption, account=lang)
    posted_key = "posted" if lang == "en" else "posted_hi"
    if media_id:
        print(f"  -> slot #{index + 1} ({lang}) is flagged Manual and a matching post "
              f"({media_id}) was found on the feed - marking it posted.")
        state[posted_key][index] = True
        state[f"last_post_time{'' if lang == 'en' else '_hi'}"] = now_ist().isoformat()
        _save_state_remote(state)
    else:
        print(f"  -> slot #{index + 1} ({lang}) is flagged Manual and still overdue - "
              f"no matching post on the feed yet, leaving it pending.")


def _fire_due_slot_for_lang(state: dict, lang: str, manual_indices: set) -> bool:
    """
    Runs the "find earliest overdue slot, respect the per-account gap,
    publish it" logic for ONE language, mutating `state` in place.
    Returns True if a post was actually fired (so the caller knows to
    persist state). en and hi are fired independently within the same
    check_once() call - at most one post per language per invocation,
    same as the original single-language behavior, just doubled.
    """
    times_key = "planned_times" if lang == "en" else "planned_times_hi"
    posted_key = "posted" if lang == "en" else "posted_hi"
    last_post_key = "last_post_time" if lang == "en" else "last_post_time_hi"

    now = now_ist()
    due_index = None
    for i, iso_time in enumerate(state[times_key]):
        if state[posted_key][i]:
            continue
        if now < datetime.fromisoformat(iso_time):
            continue
        if i in manual_indices:
            try:
                _check_manual_slot(state, i, lang)
            except Exception as e:
                print(f"[{now.isoformat()}] Failed checking manual slot #{i + 1} ({lang}) "
                      f"against the live feed, will retry next check: {e}")
            continue  # never auto-post a manual slot - just check if you posted it yourself
        due_index = i
        break

    if due_index is None:
        return False

    last_post_iso = state.get(last_post_key)
    last_post_dt = datetime.fromisoformat(last_post_iso) if last_post_iso else None
    gap_ok = last_post_dt is None or (now - last_post_dt).total_seconds() >= MIN_GAP_MINUTES * 60
    if not gap_ok:
        print(f"[{now.isoformat()}] Slot #{due_index + 1} ({lang}) is overdue but the "
              f"minimum gap since the last {lang} post hasn't elapsed yet - waiting for a later run.")
        return False

    planned_dt = datetime.fromisoformat(state[times_key][due_index])
    print(f"[{now.isoformat()}] Firing scheduled {lang} post "
          f"#{due_index + 1}/{len(state[times_key])} "
          f"(planned {planned_dt.strftime('%H:%M')} IST)")

    try:
        prebuilt = _get_prebuilt_slot(due_index)
        if prebuilt:
            _publish_prebuilt_slot(prebuilt, lang)
        else:
            print(f"  -> no pre-built content found for this slot - building fresh now ({lang}).")
            hourly_run.run_combined(
                story_count=STORIES_PER_POST,
                images_per_story=IMAGES_PER_STORY,
                apply_jitter=False,
                account=lang,
            )
    except Exception:
        print(f"Post attempt failed ({lang}) - will not retry this slot, "
              f"continuing with the rest of today's schedule.")
        traceback.print_exc()

    state[posted_key][due_index] = True
    state[last_post_key] = now_ist().isoformat()
    return True


def check_once():
    """
    One-shot version of run_forever()'s loop body, meant to be invoked by
    an external scheduler (GitHub Actions cron, e.g. every ~30 minutes)
    instead of running as a resident process. State lives in Supabase
    (app_state, key "scheduler_state") rather than a local JSON file,
    since GitHub Actions runners don't persist a filesystem between runs.
    All "today"/"now" here means IST, regardless of the server's own
    clock.

    Fires AT MOST ONE post PER LANGUAGE per invocation - up to two posts
    total (one en, one hi), each the earliest overdue not-yet-posted
    slot for that language that also clears MIN_GAP_MINUTES since the
    last post on that same account - then exits. Safe to call as often
    as you like; it's a no-op for a language if nothing is currently due
    for it.

    If content_pregen.py already built a slot's carousel for a given
    language, that pre-built content is published as-is. Otherwise this
    builds fresh content on the spot for that language, exactly as
    before this feature existed.

    Slots flagged Manual in the PWA (see slot_overrides.manual_en /
    manual_hi) are never auto-posted for that language. Instead, for
    each overdue manual slot, this checks whether a matching post
    already appeared on that language's real Instagram feed and marks
    it done if so.
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

    manual_by_lang = {}
    for lang in LANGS:
        try:
            # NOTE: assumes get_manual_slot_indices(date, lang=...) - see
            # the note at the bottom of this file if supabase_client.py
            # doesn't support this yet.
            manual_by_lang[lang] = get_manual_slot_indices(state["date"], lang=lang)
        except Exception as e:
            manual_by_lang[lang] = set()
            print(f"[{now_ist().isoformat()}] Failed to fetch manual slot indices for {lang}, "
                  f"treating as none: {e}")

    fired_any = False
    for lang in LANGS:
        try:
            if _fire_due_slot_for_lang(state, lang, manual_by_lang[lang]):
                fired_any = True
        except Exception:
            print(f"[{now_ist().isoformat()}] Unexpected error while checking/firing the "
                  f"{lang} slot - continuing with the other language.")
            traceback.print_exc()

    if not fired_any:
        print(f"[{now_ist().isoformat()}] Nothing due for auto-posting right now. "
              f"en: {sum(state['posted'])}/{len(state['posted'])}, "
              f"hi: {sum(state['posted_hi'])}/{len(state['posted_hi'])} posted today.")

    try:
        _save_state_remote(state)
    except Exception as e:
        print(f"[{now_ist().isoformat()}] Failed to save updated state to Supabase after this "
              f"check - if a post fired above, the next run may re-attempt that slot. Manually "
              f"verify before assuming a duplicate is coming: {e}")
        traceback.print_exc()


def run_forever():
    """
    Local always-on mode. NOTE: this mode still runs off local
    scheduler_state.json and has NOT been updated for dual-track en/hi
    scheduling in this pass - it still posts English only, the same as
    before. Use `--check-once` (Supabase-backed) for Hindi publishing;
    if you rely on run_forever() in production, say so and I'll port the
    same dual-track logic into this loop too.
    """
    raise NotImplementedError(
        "run_forever() (local JSON state, always-on mode) hasn't been ported to dual-track "
        "en/hi scheduling yet - only check_once() (Supabase-backed, e.g. GitHub Actions cron) "
        "has the Hindi fix in this pass. If you actually run this process as a long-lived "
        "always-on script rather than via cron, tell me and I'll port run_forever() too."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check-once", action="store_true",
        help="Run one check-and-maybe-post pass (both languages) against Supabase-backed "
             "state, then exit. Use this mode when invoked by an external scheduler like "
             "GitHub Actions cron.",
    )
    args = parser.parse_args()

    if args.check_once:
        check_once()
    else:
        run_forever()
