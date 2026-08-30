import os
from daily_scheduler import today_ist, slots_key, _load_state_remote
from supabase_client import get_state

today_str = today_ist().isoformat()
print("Server today_ist():", today_str)

sched = _load_state_remote()
print("scheduler_state date:", sched.get("date"))
print("scheduler_state slot count (en/hi):", len(sched.get("planned_times", [])), "/", len(sched.get("planned_times_hi", [])))

slots_row = get_state(slots_key(today_str), default=None)
if slots_row is None:
    print(f"daily_slots:{today_str} -> ROW DOES NOT EXIST")
else:
    print(f"daily_slots:{today_str} date field:", slots_row.get("date"))
    print(f"daily_slots:{today_str} slot count:", len(slots_row.get("slots", [])))
    indices = sorted(s.get("index") for s in slots_row.get("slots", []))
    print("slot indices present:", indices)
    missing = [i for i in range(len(sched.get("planned_times", []))) if i not in indices]
    print("indices in schedule but MISSING from daily_slots:", missing)
