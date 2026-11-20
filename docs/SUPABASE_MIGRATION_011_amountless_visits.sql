-- Pick3 migration 011: visits without an amount (나의건강기록 진료이력 has dates, providers and 상병명 but no
-- money). Safe to re-run. The amount stays empty until a receipt / 요양급여내역 for the same visit fills it.
-- Totals on the 의료비 page and in 보장분석 ignore empty amounts.
alter table public.pick3_medical_records alter column amount drop not null;
