create table api_keys (
  id bigserial primary key,
  hash text not null unique,
  prefix text not null,
  tenant text not null,
  created_at timestamptz not null default now(),
  revoked_at timestamptz
);

create table forecast_runs (
  id bigserial primary key,
  tenant text not null,
  model text not null,
  horizon int not null,
  context_len int not null,
  n_past_cov int not null default 0,
  n_future_cov int not null default 0,
  latency_ms real not null,
  created_at timestamptz not null default now()
);

create index on forecast_runs (tenant, created_at desc);
