-- Enforce the existing phone body bound even when a caller supplies JSON null.
alter table public.mr_market_source_captures drop constraint mr_live_capture_compressed_limit;
alter table public.mr_market_source_captures add constraint mr_live_capture_compressed_limit
  check (owner_id is null or
    (original_body is null and result <> 'RECEIVED') or
    (original_body is not null and stored_bytes is not null and
     stored_bytes = octet_length(original_body) and stored_bytes between 1 and 4194304));
