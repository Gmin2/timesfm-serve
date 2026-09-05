create table weather_live_inputs (
  id uuid primary key,
  station_id text not null check (station_id in ('42410099999', '43128599999', '43279099999')),
  forecast_origin timestamptz not null,
  captured_at timestamptz not null,
  issue_deadline timestamptz not null,
  document jsonb not null,
  sha256 text not null,
  unique (station_id, forecast_origin),
  check (captured_at > forecast_origin and captured_at <= issue_deadline)
);

alter table weather_jobs add column kind text not null default 'replay' check (kind in ('replay', 'live'));
alter table weather_jobs add column input_snapshot_id uuid references weather_live_inputs(id);
alter table weather_jobs alter column account_id drop not null;
alter table weather_jobs drop constraint weather_jobs_points_check;
alter table weather_jobs add constraint weather_job_ownership check (
  (kind = 'replay' and account_id is not null and input_snapshot_id is null and points = 48) or
  (kind = 'live' and account_id is null and input_snapshot_id is not null and points = 0)
);
create unique index weather_live_job_once on weather_jobs (input_snapshot_id) where kind = 'live';

create table weather_ingestion_status (
  station_id text primary key check (station_id in ('42410099999', '43128599999', '43279099999')),
  forecast_origin timestamptz not null,
  attempted_at timestamptz not null default clock_timestamp(),
  status text not null check (status in ('queued', 'existing', 'data_rejected', 'provider_unavailable', 'queue_full')),
  error_code text
);
