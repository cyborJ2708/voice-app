-- Subscription + quota state, one row per user. Single source of truth for
-- plan and usage — replaces the old ad-hoc "subscriptions" table that /api/me
-- used to read from (plan/status only, no quota tracking).
--
-- period_start anchors the free-tier weekly quota to Monday 00:00 IST (see
-- main.py's _current_period_start_ist / _maybe_reset_usage for the actual
-- reset logic — this table just stores the result, the backend owns the
-- reset decision).

create table if not exists user_usage (
  user_id uuid primary key references auth.users(id) on delete cascade,
  plan text not null default 'free' check (plan in ('free', 'pro')),
  words_used_this_period integer not null default 0,
  period_start timestamptz not null default now(),
  subscription_status text,
  subscription_expires_at timestamptz,
  razorpay_subscription_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table user_usage enable row level security;

-- Users can read their own row (dashboard "words remaining" display) but
-- can never write to it directly — plan/usage changes only ever happen via
-- the backend's service-role key (quota increments, webhook-driven plan
-- upgrades), never from the browser or desktop app's own Supabase session.
create policy "user_usage_select_own"
  on user_usage for select
  using (auth.uid() = user_id);

-- No insert/update/delete policies for the authenticated/anon roles at all —
-- RLS defaults to deny, and only the service-role key (which bypasses RLS
-- entirely) can write. This is intentional, not an oversight.
