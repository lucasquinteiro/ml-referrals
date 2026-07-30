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
    id              bigserial primary key,
    product_id      text not null,        -- the offer this tweet promotes
    posted_at       timestamptz not null default now(),
    tweet_id        text,                 -- X status id ('slack' for the simulator)
    tweet_url       text,                 -- canonical permalink (real X posts only)
    tweet_text      text,
    affiliate_url   text,                 -- exact link that went out
    target          text,                 -- twitter_api | twitter_cookie | slack
    title           text,                 -- product title at post time
    matched_label   text,                 -- offer category
    matched_keyword text,                 -- keyword that surfaced it
    price           numeric,
    original_price  numeric,
    currency        text,
    discount_pct    integer,
    char_count      integer,              -- tweet length as X counts it
    has_image       boolean default false,
    dry_run         boolean default false
);
create index if not exists idx_mlr_posts_product on mlr_posts (product_id, posted_at desc);

-- Backfill the richer post-log columns on a project created before they existed.
-- Safe to re-run; existing rows keep NULL for metadata not captured at the time.
alter table mlr_posts add column if not exists tweet_url       text;
alter table mlr_posts add column if not exists target          text;
alter table mlr_posts add column if not exists title           text;
alter table mlr_posts add column if not exists matched_label   text;
alter table mlr_posts add column if not exists matched_keyword text;
alter table mlr_posts add column if not exists original_price  numeric;
alter table mlr_posts add column if not exists currency        text;
alter table mlr_posts add column if not exists char_count      integer;
alter table mlr_posts add column if not exists has_image       boolean default false;

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
