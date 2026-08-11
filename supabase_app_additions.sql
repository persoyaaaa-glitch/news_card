-- Run this in Supabase's SQL Editor (Dashboard -> SQL Editor -> New query),
-- AFTER supabase_setup.sql. This adds what the companion PWA needs.
--
-- Two things happen here:
--   1. A new table to store each device's Web Push subscription.
--   2. Row-Level Security so the PWA's anon key (which is safe to embed
--      in client-side JS, unlike the service_role key) can ONLY do two
--      things: insert a push subscription, and read today's
--      pre-generated slot content. It can't read/write anything else -
--      not posted_articles, not the rest of app_state, nothing.
--
-- Your Python scripts (which use the service_role key) are unaffected by
-- any of this - service_role always bypasses RLS entirely.

create table if not exists push_subscriptions (
    id bigint generated always as identity primary key,
    endpoint text not null unique,
    p256dh text not null,
    auth text not null,
    created_at timestamptz not null default now()
);

alter table push_subscriptions enable row level security;

-- The PWA inserts its own subscription once, on first "allow notifications".
-- It never needs to read the list back or delete others' subscriptions.
create policy "anon can register a push subscription"
    on push_subscriptions for insert
    to anon
    with check (true);

-- Re-subscribing (e.g. browser rotated the endpoint) should update, not
-- error on the unique constraint.
create policy "anon can update its own subscription row"
    on push_subscriptions for update
    to anon
    using (true)
    with check (true);

-- Lock down app_state so the anon key can only ever read the daily_slots
-- rows the PWA actually needs (one per date - see daily_scheduler.py's
-- slots_key() - so it can show a rolling window of past days, not just
-- today) - nothing else in that table (scheduler_state, token_state,
-- etc.) is exposed.
--
-- NOTE: if you already ran an older version of this file, its policy
-- only covered the single old `key = 'daily_slots'` row and won't match
-- the new per-date keys - run migration_daily_slots_history.sql instead
-- of re-running this block.
alter table app_state enable row level security;

create policy "anon can read daily_slots history rows"
    on app_state for select
    to anon
    using (key like 'daily_slots:%');

create policy "anon can read only the repo_traffic row"
    on app_state for select
    to anon
    using (key = 'repo_traffic');

-- posted_articles gets RLS enabled with NO anon policies at all, i.e.
-- fully locked to the service_role key only (your Python scripts).
alter table posted_articles enable row level security;
