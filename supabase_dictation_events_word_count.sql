-- Adds word_count to dictation_events (Stage 5 follow-up) — still metadata
-- only: a word *count* is not the transcript text itself. Run once in the
-- Supabase SQL editor.

alter table public.dictation_events
  add column if not exists word_count integer;
