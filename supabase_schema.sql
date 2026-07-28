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

-- Links minted by MercadoLibre's link builder. Cached because they're stable
-- per (product, tag) and each one costs a round trip through a real browser.
create table if not exists mlr_affiliate_links (
    product_id  text primary key,
    product_url text not null,
    short_url   text,
    full_url    text,
    tag         text,
    created_at  timestamptz not null default now()
);
create index if not exists idx_mlr_affiliate_links_tag
    on mlr_affiliate_links (tag);

-- Offer-card images rendered locally and uploaded to Supabase Storage, so the
-- GitHub posting job (which can't see your disk) can attach them.
-- Also create a PUBLIC storage bucket named "offer-cards".
create table if not exists mlr_offer_images (
    product_id text primary key,
    url        text not null,
    local_path text,
    created_at timestamptz not null default now()
);
