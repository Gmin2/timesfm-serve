-- Day-ahead IEX price forecasts, stored at issue time so the track record is
-- a record and not a claim. One row per delivery day per model; the 96 blocks
-- and their quantiles live in the payload.
create table if not exists iex_forecasts (
    id bigserial primary key,
    delivery_date date not null,
    model text not null,
    issued_at timestamptz not null default now(),
    -- the information cutoff the forecast was built under, 09:30 IST on D-1
    cutoff_at timestamptz not null,
    blocks jsonb not null,
    revision text not null,
    unique (delivery_date, model)
);

create index if not exists iex_forecasts_delivery on iex_forecasts (delivery_date desc);

-- Scores are written later, once the delivery day has settled and Grid-India
-- has published the prices back. Kept separate from the forecast so a score can
-- never be written in the same breath as the thing it scores.
create table if not exists iex_scores (
    forecast_id bigint primary key references iex_forecasts (id) on delete cascade,
    scored_at timestamptz not null default now(),
    blocks_scored int not null,
    mae double precision not null,
    rmse double precision not null,
    bias double precision not null,
    coverage_p10_p90 double precision
);
