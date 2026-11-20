-- Pick3 migration 009: planner-confirmed contract status per 보장분석 report. Safe to re-run.
-- {"14": "stopped", "6": "paid_up"}: paying | paid_up | stopped (해지/유예/완납 미확인) | suspended | terminated.
-- Contracts not listed keep Pick3's estimate (from 납입 횟수 vs 경과 개월).
alter table public.pick3_coverage_reports add column if not exists overrides jsonb not null default '{}'::jsonb;
