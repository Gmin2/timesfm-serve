-- The runtime roles get no rights on a new table just because it exists. The API
-- reads forecasts and never writes them, so it gets select only; the scheduled
-- jobs that issue and settle forecasts get the writes.
--
-- Wrapped in a role check because local development runs as a single superuser
-- and has none of these roles.
do $$
begin
    if exists (select 1 from pg_roles where rolname = 'weather_api') then
        grant select on iex_forecasts, iex_scores to weather_api;
    end if;
    if exists (select 1 from pg_roles where rolname = 'weather_ingest') then
        grant select, insert on iex_forecasts, iex_scores to weather_ingest;
        grant usage, select on sequence iex_forecasts_id_seq to weather_ingest;
    end if;
end
$$;
