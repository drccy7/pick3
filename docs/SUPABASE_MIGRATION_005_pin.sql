-- Pick3 migration 005: name + PIN sign-in for the web pages. Run once in Supabase SQL Editor. Safe to re-run.
-- Personal tokens (Shortcuts) keep working unchanged.

-- 1) Each user may set a 6-digit PIN (stored only as a salted PBKDF2 hash) with a lockout counter.
alter table public.pick3_users add column if not exists pin_hash text;
alter table public.pick3_users add column if not exists pin_failed int not null default 0;
alter table public.pick3_users add column if not exists pin_locked_until timestamptz;

-- 2) Web sessions created by a successful name + PIN sign-in (one row per browser).
create table if not exists public.pick3_sessions (
  token_hash text primary key,
  user_id bigint not null references public.pick3_users(id) on delete cascade,
  created_at timestamptz not null default now()
);
create index if not exists pick3_sessions_user_idx on public.pick3_sessions (user_id);
alter table public.pick3_sessions enable row level security;
