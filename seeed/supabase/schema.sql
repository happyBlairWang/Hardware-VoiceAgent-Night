-- Notetaker storage. Paste into the Supabase SQL editor and run once.
--
-- Design notes:
--   * transcripts are text, so they are tiny. A one-hour meeting is roughly
--     60 KB. The 500 MB free tier holds something like 8,000 hours.
--   * audio never comes here. It stays transient - card, then Mac, then gone.
--   * full-text search is built in below with tsvector. No embedding model, no
--     extra service, no API key. Add pgvector later only if FTS stops being
--     enough (see the bottom of this file).

create table if not exists meetings (
  id             uuid primary key default gen_random_uuid(),
  recorded_at    timestamptz not null,
  duration_secs  int,
  source         text check (source in ('pendant', 'mac')) default 'mac',
  title          text,
  summary        text,
  transcript     text,
  -- kept for reference even after the transcript is deleted on their side
  assemblyai_id  text,
  created_at     timestamptz not null default now()
);

-- One row per speaker turn. Lets you answer "what did Alex actually say"
-- without regex over a blob of text.
create table if not exists utterances (
  id          bigserial primary key,
  meeting_id  uuid not null references meetings(id) on delete cascade,
  speaker     text,
  start_ms    int,
  end_ms      int,
  text        text not null
);

create index if not exists utterances_meeting_idx on utterances(meeting_id);
create index if not exists meetings_recorded_idx  on meetings(recorded_at desc);

-- Search across every meeting. Generated column, so it can never drift out of
-- sync with the transcript the way a trigger-maintained one can.
alter table meetings drop column if exists fts;
alter table meetings add column fts tsvector
  generated always as (
    to_tsvector('english',
      coalesce(title, '') || ' ' || coalesce(summary, '') || ' ' || coalesce(transcript, ''))
  ) stored;

create index if not exists meetings_fts_idx on meetings using gin(fts);

-- Lock it down. Without this, anyone holding the anon key reads every meeting
-- you have ever recorded.
alter table meetings   enable row level security;
alter table utterances enable row level security;

-- Single-user setup: the relay uses the SERVICE key, which bypasses RLS.
-- No policies are granted to anon on purpose - the anon key can read nothing.
-- If this ever becomes multi-user, add an owner uuid column and policies here.

--  what did we decide about the battery?
--    select title, recorded_at, ts_headline('english', transcript, q) as hit
--    from meetings, websearch_to_tsquery('english', 'battery decision') q
--    where fts @@ q
--    order by ts_rank(fts, q) desc;

--  semantic search, later, only if you need it:
--    create extension if not exists vector;
--    alter table meetings add column embedding vector(1536);
--    create index on meetings using hnsw (embedding vector_cosine_ops);
