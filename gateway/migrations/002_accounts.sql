create table accounts (
  id bigserial primary key,
  name text not null unique,
  email text,
  user_id text unique,
  credits_granted bigint not null default 50000,
  credits_used bigint not null default 0,
  rate_limit_per_min int not null default 60,
  created_at timestamptz not null default now(),
  suspended_at timestamptz
);

alter table api_keys add column account_id bigint references accounts(id) on delete cascade;
alter table api_keys add column label text;

insert into accounts (name) select distinct tenant from api_keys;
update api_keys k set account_id = a.id from accounts a where a.name = k.tenant;

alter table api_keys alter column account_id set not null;
alter table api_keys drop column tenant;

alter table forecast_runs add column account_id bigint references accounts(id);
alter table forecast_runs add column points int;
alter table forecast_runs add column request_id text;
update forecast_runs r set account_id = a.id from accounts a where a.name = r.tenant;
alter table forecast_runs drop column tenant;

create table rate_limit_counters (
  key_hash text not null,
  window_start timestamptz not null,
  count int not null default 0,
  primary key (key_hash, window_start)
);

create index on rate_limit_counters (window_start);
