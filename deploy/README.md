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
| `frontend.sh` | Local frontend build and prebuilt Vercel production deployment |
| `build_source.py` | Allowlisted, content-addressed backend source ZIP for CodeBuild |

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

API and bootstrap images use digest-pinned Chainguard Python 3.14 build/runtime
stages. Only the virtual environment and application files enter the runtime;
there is no shell or package manager. They run as UID 10001 with a read-only root
filesystem. The API's Docker health check uses exec form. Worker and ingestion
images retain their separate Python/CUDA bases. Image promotion requires both the
Trivy and ECR HIGH/CRITICAL gates to pass; unfixed findings are not ignored.

Package a backend build without local environment files or credentials:

```bash
.venv/bin/python deploy/build_source.py --output tmp/build-source
```

Upload the returned ZIP to the deployment's S3 artifact bucket under its returned
`builds/<SHA256>.zip` key. Set `build_source_key` in the ignored runtime tfvars and
review the Terraform plan before applying. An account-only CodeBuild release runs
`bash scripts/weather_build_images.sh api bootstrap`; do not build or redeploy the
GPU worker just to activate customer login.

`rds-global-bundle.pem` is the public AWS RDS certificate bundle downloaded from
https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem on 2026-09-05.
It contains public trust certificates, not private keys or credentials. Review CA
updates before later deployments. Runtime database connections use `verify-full`.

`nvidia-device-plugin.yaml` adapts NVIDIA's v0.20.0 static manifest with a pinned
image digest and this pilot's GPU-node placement. Its Apache-2.0 license is included
as `LICENSE.nvidia-device-plugin`.

## Customer Access Rollout

Local setup is in `frontend/README.md`. Use the following order for an AWS account
release; its migration is additive and does not require a GPU worker rollout:

1. The dashboard is hosted at `https://timesfms.vercel.app`. Register
   the production GitHub OAuth app with callback
   `https://timesfms.vercel.app/api/auth/github/callback`. The backend uses this fixed
   origin, never the incoming Host header, for redirects and CSRF checks.
2. Enable `enable_github_oauth` in the runtime infrastructure. This provisions
   the secret container, an API-only read policy and endpoint access, not the
   secret value. Store the plain client secret with Secrets Manager's
   `put-secret-value --secret-string file://<private-file>` outside Terraform.
   The API init container writes it to `/run/weather/github-client-secret` with
   mode 0600. The main API receives only that file, not Pod Identity credentials.
   Do not put secret values in Terraform variables/state, ConfigMaps, frontend
   environment variables, image layers, or rendered JSON.
3. Add `github_oauth` to the ignored deployment JSON, with only `client_id`,
   `secret_arn` and `egress_cidrs`. Set `dashboard_origin` and the infrastructure's
   HTTPS `api_url`. Fetch the current IPv4 union of `web` and `api` from
   [GitHub's live metadata API](https://api.github.com/meta), review it and use it
   for `egress_cidrs`. The renderer creates `weather-api-github`, allowing only
   TCP 443 to those ranges. Refresh and reapply this policy when GitHub changes
   its ranges; stale entries can break sign-in. Keep other workload egress unchanged.
4. Run the updated operator migration/provisioning job before the API rollout.
   `004_dashboard_auth.sql` adds OAuth attempts, hashed dashboard sessions,
   GitHub display handles and read-only key metadata. API role grants are
   extended for these tables and account/key sequences; DDL remains operator-only.
   Render using the newly scanned API/bootstrap digests, apply `02-migrate.json`,
   wait for completion, then apply only the `weather-api` Deployment from
   `03-workloads.json`. Do not apply the full workload list for an account-only
   release. Wait for two healthy API replicas before enabling the frontend route.
5. The Vercel gateway in `frontend/api/gateway.ts` implements same-origin account
   and playground forwarding. Set `WEATHER_ACCOUNT_ORIGIN` in Vercel only after
   the AWS account release, then redeploy. Set the backend's dashboard origin to
   the exact production URL above. Public live reads remain disabled until a
   dedicated read-only, zero-credit `WEATHER_DASHBOARD_API_KEY` is provisioned;
   never use the operator key. SPA/API routing is in `frontend/vercel.json`.
6. Complete a real GitHub sign-in, create a key, call the AWS station/latest
   endpoints, revoke the key and verify 401, then sign out and verify the session
   is invalid. Check HTTPS cookie flags and that callback codes/keys are absent
   from request logs. Automated provider fixtures do not replace this acceptance test.

Self-service keys are read-only and receive no GPU replay budget. GitHub login
is disabled when OAuth credentials are absent. A failed API rollout can be undone
with `kubectl rollout undo deployment/weather-api -n weather`; retain the additive
schema migration. Remove the Vercel account origin and redeploy if reverting to an
API image without customer endpoints. Never roll back by deleting customer data.

## Frontend Deployment

Production: https://timesfms.vercel.app

The deployment input's `dashboard_origin` field sets the API container's
`WEATHER_DASHBOARD_ORIGIN` to this HTTPS origin. It is not a callback URL and
must not contain a path. Local development keeps its loopback origin unchanged.

The release serves verified historical forecasts, benchmarks and the run archive,
plus GitHub sign-in, self-service read-only API keys and an AWS API playground.
Anonymous live dashboard reads remain unconfigured. The account rollout does not
restart the GPU worker or enqueue inference.

The Vercel project is `forecast-lab` in `min2whos-projects`. Authenticate with the
Vercel CLI, then from `frontend/` link a fresh checkout:

```bash
vercel link --yes --project forecast-lab --scope min2whos-projects
```

From the repository root:

```bash
bash deploy/frontend.sh
```

This pulls production settings, builds locally and uploads only prebuilt output.
The local build is required because the data exporter reads verified artifacts
outside `frontend/`. No Git integration is enabled yet. Python 3.12+, Node.js 24
and pnpm 11.5 are required. Vercel handles HTTPS; no custom domain was purchased.
Keep the `.vercel/` and `.env*` files generated by its CLI out of Git.
