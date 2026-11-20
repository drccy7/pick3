-- Pick3 migration 008: 건강보험 요양급여내역 fields on medical records (진단명·입원/외래·일수). Safe to re-run.
-- Filled when a user shares the 국민건강보험공단 요양급여내역 PDF; existing rows of the same date and provider
-- get the diagnosis attached instead of a duplicate row. 상병명 may include 의증/배제상병 (NHIS note).
alter table public.pick3_medical_records add column if not exists diagnosis_code text;   -- KCD, e.g. I109
alter table public.pick3_medical_records add column if not exists diagnosis_name text;
alter table public.pick3_medical_records add column if not exists visit_type text;       -- 외래 | 입원 | 약국
alter table public.pick3_medical_records add column if not exists visit_days int;        -- 입내원일수
alter table public.pick3_medical_records add column if not exists care_days int;         -- 요양(투약)일수
alter table public.pick3_medical_records add column if not exists nhis_paid int;         -- 공단부담금
create index if not exists pick3_medical_records_diag_idx
  on public.pick3_medical_records (user_id, diagnosis_code) where diagnosis_code is not null;
