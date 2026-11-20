-- Pick3 migration 007: Gemini token usage per call (for cost estimates). Run once in Supabase SQL Editor. Safe to re-run.
create table if not exists public.pick3_gemini_usage (
  id bigint generated always as identity primary key,
  user_id bigint references public.pick3_users(id) on delete set null,
  feature text not null,          -- restaurant | medical | prescription | router | coverage_extract | coverage_analyze
  model text not null,            -- the model version Gemini reports (e.g. what gemini-pro-latest resolved to)
  ref text,                       -- e.g. coverage:12 (one 보장분석 run)
  prompt_tokens int not null default 0,
  output_tokens int not null default 0,
  thoughts_tokens int not null default 0,
  cached_tokens int not null default 0,
  created_at timestamptz not null default now()
);
create index if not exists pick3_gemini_usage_user_idx on public.pick3_gemini_usage (user_id, created_at desc);
create index if not exists pick3_gemini_usage_ref_idx on public.pick3_gemini_usage (ref);
alter table public.pick3_gemini_usage enable row level security;  -- no policies: server key only
