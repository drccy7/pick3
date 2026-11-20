-- Pick3 migration 006: 보장분석 보고서 보관 + 제안서 결과. Run once in Supabase SQL Editor. Safe to re-run.
-- The original PDF (still encrypted with the customer's password, if it was) and the result PDFs live in a
-- PRIVATE Storage bucket; only the server (secret key) reads them. Passwords are never stored.

-- 1) One row per shared report.
create table if not exists public.pick3_coverage_reports (
  id bigint generated always as identity primary key,
  user_id bigint not null references public.pick3_users(id) on delete cascade,
  filename text,
  size_bytes int,
  encrypted boolean not null default false,
  storage_path text not null,            -- original PDF in bucket pick3-coverage
  status text not null default 'stored', -- stored | running | done | error | other
  message text,
  customer_name text,                    -- filled after the first read
  birth_date date,
  records_count int,
  extract jsonb,                         -- report read by Gemini (reused on re-runs, no password needed)
  analysis jsonb,
  plan jsonb,
  warnings jsonb,
  results jsonb,                         -- [{"name": "...pdf", "path": "user/id/analysis.pdf"}]
  created_at timestamptz not null default now(),
  analyzed_at timestamptz
);
create index if not exists pick3_coverage_reports_user_idx on public.pick3_coverage_reports (user_id, created_at desc);
alter table public.pick3_coverage_reports enable row level security;  -- no policies: server key only

-- 2) Private bucket for the PDFs (50 MB per file). Results are HTML while the server browser is off.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('pick3-coverage', 'pick3-coverage', false, 52428800, array['application/pdf', 'text/html'])
on conflict (id) do nothing;
