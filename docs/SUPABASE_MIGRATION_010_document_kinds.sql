-- Pick3 migration 010: one inbox for shared documents. Safe to re-run.
-- kind: locked (암호 문서함: password not entered yet, or opened but not classified)
--       coverage (보장분석) | checkup (건강검진) | done (already processed into 의료비 기록)
-- suggested: Pick3's guess after the password is entered (coverage | medical | checkup), the user confirms.
alter table public.pick3_coverage_reports add column if not exists kind text not null default 'coverage';
alter table public.pick3_coverage_reports add column if not exists suggested text;

-- Encrypted PDFs not read yet move to the 암호 문서함; ones already handled as 의료비 are done.
update public.pick3_coverage_reports set kind = 'done' where status = 'other';
update public.pick3_coverage_reports set kind = 'locked'
 where encrypted and extract is null and status <> 'other' and kind = 'coverage';

create index if not exists pick3_coverage_reports_user_kind
    on public.pick3_coverage_reports (user_id, kind, created_at desc);
