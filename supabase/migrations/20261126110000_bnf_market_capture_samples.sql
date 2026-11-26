-- One-time and future BNF source evidence. Existing chain_* tables remain untouched.
-- Source bytes stay exact; for sustained capture use an immutable private Storage object
-- and keep only its hash/path here. This migration does not enable a collector.
create table public.mr_market_source_captures (
  capture_id text primary key,
  source text not null,
  endpoint text not null,
  request_params jsonb not null default '{}'::jsonb,
  session_date date,
  instrument_key text,
  contract_expiry date,
  request_started_at timestamptz,
  response_received_at timestamptz not null,
  first_seen_at timestamptz not null default clock_timestamp(),
  http_status integer not null check (http_status between 0 and 599),
  result text not null check (result in ('RECEIVED','HTTP_ERROR','NETWORK_ERROR','INVALID_BODY')),
  record_count integer check (record_count >= 0),
  original_bytes integer check (original_bytes >= 0),
  original_sha256 text check (original_sha256 ~ '^[a-f0-9]{64}$'),
  original_body bytea,
  storage_bucket text,
  storage_object text,
  storage_sha256 text check (storage_sha256 ~ '^[a-f0-9]{64}$'),
  source_time timestamptz,
  notes jsonb not null default '{}'::jsonb,
  check (result <> 'RECEIVED' or (http_status = 200 and original_sha256 is not null
    and (original_body is not null or (storage_bucket is not null and storage_object is not null and storage_sha256 is not null)))),
  check (original_body is null or original_bytes = octet_length(original_body)),
  check (storage_bucket is null or storage_bucket = 'mr-private-state')
);
create index mr_market_source_captures_time on public.mr_market_source_captures(session_date, source, response_received_at desc);
create index mr_market_source_captures_expiry on public.mr_market_source_captures(contract_expiry, source, response_received_at desc);
alter table public.mr_market_source_captures enable row level security;
revoke all on public.mr_market_source_captures from public, anon, authenticated;

create table public.mr_market_capture_coverage (
  coverage_id text primary key,
  session_date date not null,
  captured_at timestamptz not null default clock_timestamp(),
  cadence text not null,
  expected jsonb not null,
  received jsonb not null,
  gaps jsonb not null default '[]'::jsonb,
  status text not null check (status in ('COMPLETE_SAMPLE','INCOMPLETE','UNVERIFIED')),
  notes text
);
alter table public.mr_market_capture_coverage enable row level security;
revoke all on public.mr_market_capture_coverage from public, anon, authenticated;

create table public.mr_bnf_weight_publications (
  publication_id text primary key,
  as_of_date date not null,
  first_seen_at timestamptz not null default clock_timestamp(),
  source_url text not null,
  source_sha256 text not null check (source_sha256 ~ '^[a-f0-9]{64}$'),
  source_pdf bytea,
  membership_sha256 text not null check (membership_sha256 ~ '^[a-f0-9]{64}$'),
  expected_count integer not null check (expected_count > 0),
  total_weight_pct numeric(10,4) not null,
  status text not null check (status in ('VALIDATED','PENDING_SOURCE','QUARANTINED')),
  check (source_pdf is not null or status <> 'VALIDATED')
);
alter table public.mr_bnf_weight_publications enable row level security;
revoke all on public.mr_bnf_weight_publications from public, anon, authenticated;

create table public.mr_bnf_weight_members (
  publication_id text not null references public.mr_bnf_weight_publications(publication_id),
  symbol text not null,
  isin text not null,
  upstox_instrument_key text not null,
  weight_pct numeric(10,4) not null check (weight_pct > 0 and weight_pct <= 100),
  primary key (publication_id, symbol),
  unique (publication_id, isin),
  unique (publication_id, upstox_instrument_key)
);
alter table public.mr_bnf_weight_members enable row level security;
revoke all on public.mr_bnf_weight_members from public, anon, authenticated;
