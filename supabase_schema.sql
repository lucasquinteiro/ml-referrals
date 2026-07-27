-- Tables for ml-referrals. All `mlr_`-prefixed because the Supabase project is
-- shared with the youtube / twitter-updates pipelines.
--
-- Apply by pasting into the Supabase SQL editor, or:
--   psql "$SUPABASE_DB_URL" -f supabase_schema.sql

create table if not exists mlr_products (
    product_id      text primary key,
    title           text not null,
    url             text not null,
    image           text,
    seller          text,
    matched_keyword text,
    matched_label   text,
    first_seen      timestamptz not null default now(),
    last_seen       timestamptz not null default now()
);

create table if not exists mlr_price_history (
    id             bigserial primary key,
    product_id     text not null references mlr_products(product_id),
    observed_at    timestamptz not null default now(),
    price          numeric,
    original_price numeric,
    discount_pct   integer,
    currency       text default 'ARS',
    badge          text,
    free_shipping  boolean default false,
    rating         numeric,
    run_id         bigint
);
create index if not exists idx_mlr_price_history_product
    on mlr_price_history (product_id, observed_at desc);

create table if not exists mlr_posts (
    id            bigserial primary key,
    product_id    text not null,
    posted_at     timestamptz not null default now(),
    tweet_id      text,
    tweet_text    text,
    affiliate_url text,
    price         numeric,
    discount_pct  integer,
    dry_run       boolean default false
);
create index if not exists idx_mlr_posts_product on mlr_posts (product_id, posted_at desc);

create table if not exists mlr_runs (
    id             bigserial primary key,
    started_at     timestamptz not null default now(),
    kind           text,
    products_seen  integer default 0,
    offers_matched integer default 0,
    note           text
);
