# Weather Deployment Assets

Deployment assets live directly in this directory. Nothing here is an authorization
to create or change AWS resources. See [infrastructure](../infra/README.md) for the
Terraform layout and safety boundaries. `infra/bootstrap/` and `infra/runtime/`
keep separate state; account-specific operating notes are not committed.

| File | Purpose |
| --- | --- |
| `Dockerfile.api` | Lightweight FastAPI service, no model dependencies |
| `Dockerfile.worker` | CUDA-only TimesFM worker with offline model artifacts |
| `Dockerfile.ingest` | Scheduled observations and guidance ingestion |
| `Dockerfile.bootstrap` | Credentials, certificates, artifacts and database setup |
| `example.json` | Non-deployable example input for the manifest renderer |
| `nvidia-device-plugin.yaml` | Pinned GPU device plugin |
| `rds-global-bundle.pem` | Public RDS TLS trust certificates |
| `rendered/` | Ignored, environment-specific generated manifests |

Run from the repository root so Docker's build context includes the application:

```bash
docker build -f deploy/Dockerfile.api -t pravah-weather-api:local .
.venv/bin/python -m scripts.weather_render \
  --config deploy/example.json --output tmp/weather-rendered --example
```

The root `docker-compose.yml` uses `deploy/Dockerfile.api` for local development.
`scripts/weather_build_images.sh` selects `deploy/Dockerfile.<component>` for the
AWS image build. Real credentials, model weights and generated manifests must not
be committed or included in Docker build contexts.

`rds-global-bundle.pem` is the public AWS RDS certificate bundle downloaded from
https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem on 2026-09-05.
It contains public trust certificates, not private keys or credentials. Review CA
updates before later deployments. Runtime database connections use `verify-full`.

`nvidia-device-plugin.yaml` adapts NVIDIA's v0.20.0 static manifest with a pinned
image digest and this pilot's GPU-node placement. Its Apache-2.0 license is included
as `LICENSE.nvidia-device-plugin`.
