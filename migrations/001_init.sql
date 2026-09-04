create table accounts (
  id bigserial primary key,
  name text not null unique,
  email text,
  github_id text unique,
  credits_granted bigint not null default 50000,
  credits_used bigint not null default 0,
  rate_limit_per_min int not null default 60,
  created_at timestamptz not null default now(),
  suspended_at timestamptz
);

-- the key column is a sha256 hash, never the key. prefix is the first few
-- characters, kept so keys can be told apart in logs and in the dashboard
-- without holding the secret.
create table api_keys (
  id bigserial primary key,
  hash text not null unique,
  prefix text not null,
  label text,
  account_id bigint not null references accounts(id) on delete cascade,
  created_at timestamptz not null default now(),
  revoked_at timestamptz
);

-- append only ledger. every credit spent has a row, so a balance that looks
-- wrong can be recomputed and traced back to the calls behind it.
create table forecast_runs (
  id bigserial primary key,
  account_id bigint references accounts(id),
  model text not null,
  horizon int not null,
  context_len int not null,
  n_past_cov int not null default 0,
  n_future_cov int not null default 0,
  points int not null default 0,
  latency_ms real not null,
  request_id text,
  created_at timestamptz not null default now()
);

create index on forecast_runs (account_id, created_at desc);

create table rate_limit_counters (
  key_hash text not null,
  window_start timestamptz not null,
  count int not null default 0,
  primary key (key_hash, window_start)
);

create index on rate_limit_counters (window_start);

create table jobs (
  id uuid primary key,
  account_id bigint not null references accounts(id) on delete cascade,
  status text not null default 'queued',
  n_series int not null,
  horizon int not null,
  points int not null default 0,
  error text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz
);

create table job_results (
  job_id uuid not null references jobs(id) on delete cascade,
  series_id text not null,
  forecast jsonb not null,
  quantiles jsonb not null,
  primary key (job_id, series_id)
);

-- sessions live in the database rather than in a signed cookie so that signing
-- out actually revokes something.
create table sessions (
  token text primary key,
  account_id bigint not null references accounts(id) on delete cascade,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null
);

create index on sessions (expires_at);
