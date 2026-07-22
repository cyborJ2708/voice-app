-- Insights/History feature (Stage 5): per-dictation metadata only — never
-- the transcript/text content itself, matching the app's "audio and text
-- are never stored" privacy invariant. Run this once in the Supabase SQL
-- editor.

create table if not exists public.dictation_events (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  outcome text not null,        -- 'success' | 'empty' | 'error'
  injection_tier text           -- 'clipboard' | 'typed' | 'card' | null (only set on success)
);

create index if not exists dictation_events_user_id_created_at_idx
  on public.dictation_events(user_id, created_at desc);

alter table public.dictation_events enable row level security;

create policy "Users can view their own dictation events"
  on public.dictation_events for select
  using (auth.uid() = user_id);

-- usage_daily already exists (from the original schema) but was only ever
-- *read* by /api/me — nothing has ever incremented dictation_count. The
-- backend now upserts it whenever a dictation event is logged, so this
-- just documents the columns it depends on existing:
--   user_id uuid, usage_date date, dictation_count int (default 0)
-- If your usage_daily table already has these, no changes needed there.
