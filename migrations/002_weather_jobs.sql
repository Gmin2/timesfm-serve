create table weather_jobs (
  id uuid primary key,
  account_id bigint not null references accounts(id) on delete cascade,
  idempotency_key text not null,
  case_id text not null,
  manifest_sha256 text not null,
  status text not null default 'queued' check (status in ('queued', 'running', 'succeeded', 'failed')),
  points int not null default 48 check (points = 48),
  refunded boolean not null default false,
  attempts int not null default 0 check (attempts between 0 and 3),
  available_at timestamptz not null default now(),
  lease_token uuid,
  lease_expires_at timestamptz,
  error_code text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  unique (account_id, idempotency_key),
  check ((status = 'running') = (lease_token is not null and lease_expires_at is not null)),
  check ((status in ('succeeded', 'failed')) = (finished_at is not null)),
  check (not refunded or status = 'failed')
);

create index weather_jobs_pending on weather_jobs (available_at, created_at) where status = 'queued';
create index weather_jobs_leases on weather_jobs (lease_expires_at) where status = 'running';
create index weather_jobs_account on weather_jobs (account_id, created_at desc);

create table weather_forecasts (
  job_id uuid primary key references weather_jobs(id) on delete cascade,
  document jsonb not null,
  published_at timestamptz not null default now()
);
