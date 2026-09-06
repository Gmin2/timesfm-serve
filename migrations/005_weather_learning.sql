create table weather_evaluations (
  job_id uuid primary key references weather_jobs(id) on delete cascade,
  checked_at timestamptz not null,
  report jsonb not null
);

create table weather_training_runs (
  id uuid primary key,
  created_at timestamptz not null default clock_timestamp(),
  finished_at timestamptz,
  dataset_sha256 text not null unique check (dataset_sha256 ~ '^[a-f0-9]{64}$'),
  status text not null check (status in ('running', 'review_required', 'rejected', 'failed', 'timed_out', 'interrupted')),
  report jsonb not null default '{}',
  artifact_prefix text
);
