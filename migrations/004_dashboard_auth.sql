alter table accounts add column github_login text;
alter table api_keys add column read_only boolean not null default false;

-- Browser credentials, unlike the legacy sessions table, are always hashed.
create table dashboard_sessions (
  token_hash text primary key,
  account_id bigint not null references accounts(id) on delete cascade,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null
);
create index on dashboard_sessions (account_id, created_at desc);
create index on dashboard_sessions (expires_at);

-- Single-use OAuth attempts work across API replicas and expire after 10 minutes.
create table github_oauth_attempts (
  state_hash text primary key,
  browser_hash text not null,
  code_verifier text not null,
  expires_at timestamptz not null
);
create index on github_oauth_attempts (expires_at);
