-- Run this in Supabase's SQL Editor (Dashboard -> SQL Editor -> New query)

create table if not exists posted_articles (
    id bigint generated always as identity primary key,
    title text not null,
    link text not null unique,
    source text,
    ig_media_id text,
    posted_at timestamptz not null default now()
);

create index if not exists idx_posted_articles_link on posted_articles (link);
create index if not exists idx_posted_articles_posted_at on posted_articles (posted_at);

-- Optional: auto-clean entries older than 30 days so the table doesn't
-- grow forever (dedup only needs to look back a reasonable window anyway).
-- You can run this manually or wire it to Supabase's pg_cron later:
-- delete from posted_articles where posted_at < now() - interval '30 days';

-- Generic key/value state store, used instead of local JSON files
-- (scheduler_state.json, theme_state.json, token_state.json) once the
-- pipeline runs on GitHub Actions - runners are ephemeral and wipe the
-- filesystem after every run, so anything that needs to persist between
-- runs (today's post schedule, theme rotation pointer, token expiry)
-- has to live somewhere external. This table is that "somewhere."
create table if not exists app_state (
    key text primary key,
    value jsonb not null,
    updated_at timestamptz not null default now()
);

