create table if not exists public.sandbox_orders (
    id bigserial primary key,
    ts timestamptz not null default now(),
    trade_ref text,
    api text not null check (api in ('place', 'multi', 'modify', 'cancel')),
    api_version text not null,
    request_json jsonb not null default '{}'::jsonb,
    response_json jsonb not null default '{}'::jsonb,
    http_status integer,
    latency_ms integer,
    order_ids jsonb not null default '[]'::jsonb,
    error_code text,
    error_message text
);

create index if not exists sandbox_orders_ts_idx
    on public.sandbox_orders (ts desc);

create index if not exists sandbox_orders_trade_ref_idx
    on public.sandbox_orders (trade_ref);

create index if not exists sandbox_orders_api_idx
    on public.sandbox_orders (api, api_version);

alter table public.sandbox_orders enable row level security;

drop policy if exists sandbox_orders_insert_anon on public.sandbox_orders;
create policy sandbox_orders_insert_anon on public.sandbox_orders
    for insert to anon
    with check (true);

drop policy if exists sandbox_orders_select_anon on public.sandbox_orders;
create policy sandbox_orders_select_anon on public.sandbox_orders
    for select to anon
    using (true);
