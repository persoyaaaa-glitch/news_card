-- Run this in Supabase's SQL Editor (Dashboard -> SQL Editor -> New query),
-- AFTER supabase_app_additions.sql.
--
-- daily_scheduler.py now writes each day's pre-generated slot content to
-- its OWN app_state row - key "daily_slots:YYYY-MM-DD" - instead of a
-- single "daily_slots" row that got overwritten every day. This is what
-- lets the PWA keep a rolling 2-day window (swipe between today and
-- yesterday) instead of only ever having today's data. See
-- daily_scheduler.py's slots_key()/_cleanup_old_daily_slots().
--
-- The original anon policy from supabase_app_additions.sql only allowed
-- reading the exact row `key = 'daily_slots'`, which no longer exists.
-- This replaces it with a pattern match on the new per-date keys, still
-- scoped to ONLY that prefix - nothing else in app_state (scheduler_state,
-- token_state, etc.) becomes readable.

drop policy if exists "anon can read only the daily_slots row" on app_state;

create policy "anon can read daily_slots history rows"
    on app_state for select
    to anon
    using (key like 'daily_slots:%');
