"""
send_notifications.py
Checked on every GitHub Actions run (same 20-min cadence as
daily_scheduler.py --check-once). Finds any scheduled slot whose post
time is 15 minutes or less away, hasn't already been notified, and
sends a Web Push notification about it to every device subscribed via
the companion PWA.

Nothing here posts to Instagram or touches the schedule itself - it
only reads today's schedule + pre-generated content (if any) and marks
which slots have already been notified about, so the same slot is
never announced twice.
"""
import json
import os

from pywebpush import webpush, WebPushException

from daily_scheduler import (
    slots_key, STATE_KEY, IST, now_ist, today_ist,
    _ensure_today_schedule_remote, _load_state_remote, _save_state_remote,
)
from supabase_client import get_client, get_state

from datetime import datetime, timedelta

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:example@example.com")

NOTIFY_WINDOW_MINUTES = 15  # send when a slot is this close (or closer) to firing


def _get_subscriptions() -> list:
    client = get_client()
    resp = client.table("push_subscriptions").select("id,endpoint,p256dh,auth").execute()
    return resp.data


def _remove_subscription(sub_id: int):
    """A subscription that 410s/404s is dead (user uninstalled the PWA,
    cleared site data, etc.) - stop trying to push to it."""
    client = get_client()
    client.table("push_subscriptions").delete().eq("id", sub_id).execute()


def _slot_summary(slots_state: dict, index: int) -> str:
    for slot in slots_state.get("slots", []):
        if slot.get("index") == index and slot.get("stories"):
            top = slot["stories"][0]
            return top["title"][:80]
    return "News caramel"


def _send_to_all(title: str, body: str, tag: str):
    if not VAPID_PRIVATE_KEY:
        print("[send_notifications] VAPID_PRIVATE_KEY not set - skipping push (add it as "
              "a GitHub secret to enable notifications).")
        return

    subs = _get_subscriptions()
    if not subs:
        print("[send_notifications] No devices are subscribed yet (open the PWA and allow "
              "notifications once).")
        return

    payload = json.dumps({"title": title, "body": body, "tag": tag})
    for sub in subs:
        subscription_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
            )
            print(f"[send_notifications] sent to subscription #{sub['id']}")
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                print(f"[send_notifications] subscription #{sub['id']} is dead - removing it")
                _remove_subscription(sub["id"])
            else:
                print(f"[send_notifications] push failed for #{sub['id']}: {e}")


def check_and_notify():
    state = _ensure_today_schedule_remote(_load_state_remote())
    slots_state = get_state(slots_key(today_ist().isoformat()), default={})
    now = now_ist()
    changed = False

    if "notified" not in state:
        state["notified"] = [False] * len(state["planned_times"])
        changed = True

    if not state.get("schedule_announced"):
        n = len(state["planned_times"])
        first = datetime.fromisoformat(state["planned_times"][0]).strftime("%H:%M")
        last = datetime.fromisoformat(state["planned_times"][-1]).strftime("%H:%M")
        print(f"[send_notifications] announcing today's schedule ({n} post(s), {first}-{last} IST)")
        _send_to_all(
            title=f"Today's schedule is ready - {n} taco(s)",
            body=f"First at {first} IST, last at {last} IST. Open the app to see all times.",
            tag=f"{state['date']}-schedule",
        )
        state["schedule_announced"] = True
        changed = True

    for i, iso_time in enumerate(state["planned_times"]):
        if state["posted"][i] or state["notified"][i]:
            continue
        planned_dt = datetime.fromisoformat(iso_time)
        minutes_away = (planned_dt - now).total_seconds() / 60
        if 0 <= minutes_away <= NOTIFY_WINDOW_MINUTES:
            headline = _slot_summary(slots_state, i)
            print(f"[send_notifications] slot #{i + 1} fires in ~{int(minutes_away)} min "
                  f"({planned_dt.strftime('%H:%M')} IST) - notifying")
            _send_to_all(
                title=f"Taco #{i + 1}/{len(state['planned_times'])} in {max(int(minutes_away), 1)} min",
                body=headline,
                tag=f"{state['date']}-{i}",
            )
            state["notified"][i] = True
            changed = True

    if changed:
        _save_state_remote(state)
    else:
        print(f"[{now.isoformat()}] No slot within the {NOTIFY_WINDOW_MINUTES}-min notify window right now.")


if __name__ == "__main__":
    check_and_notify()
