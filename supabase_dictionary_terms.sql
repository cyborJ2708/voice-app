-- Dictionary feature (Stage 4): custom terms merged into the Gemini prompt
-- so proper nouns / jargon / names get transcribed the way the user spells
-- them. Run this once in the Supabase SQL editor.

create table if not exists public.dictionary_terms (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  term text not null,
  created_at timestamptz not null default now()
);

create index if not exists dictionary_terms_user_id_idx on public.dictionary_terms(user_id);

alter table public.dictionary_terms enable row level security;

create policy "Users can view their own dictionary terms"
  on public.dictionary_terms for select
  using (auth.uid() = user_id);

create policy "Users can insert their own dictionary terms"
  on public.dictionary_terms for insert
  with check (auth.uid() = user_id);

create policy "Users can delete their own dictionary terms"
  on public.dictionary_terms for delete
  using (auth.uid() = user_id);
