-- Preserve original API bytes losslessly in gzip for the bounded manual sample.
alter table public.mr_market_source_captures
  add column body_encoding text not null default 'identity'
    check (body_encoding in ('identity', 'gzip'));
alter table public.mr_market_source_captures
  add column stored_bytes integer check (stored_bytes >= 0);
alter table public.mr_market_source_captures
  drop constraint mr_market_source_captures_check1;
-- The original length/hash describe decoded vendor bytes; stored_bytes describes
-- the compressed representation. The collector verifies both on readback.
alter table public.mr_market_source_captures
  add constraint mr_market_capture_body_length_check
  check (original_body is null or
    (stored_bytes = octet_length(original_body) and
      (body_encoding = 'gzip' or original_bytes = stored_bytes)));
