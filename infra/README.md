# AWS Infrastructure

```text
infra/
  bootstrap/   Terraform state bucket and EKS operator role
  runtime/     Network, EKS, GPU nodes, RDS, images and HTTPS entry point
```

These are separate Terraform roots, not duplicate stacks. The bootstrap resources
must survive application teardown. The runtime root receives the operator role ARN
as an input; it does not own the role or the state bucket.

The folder rename does not change AWS resource names, Terraform resource addresses
or remote state keys. The existing deployment keeps `weather/bootstrap.tfstate`
and `weather/pilot.tfstate` in the same protected state bucket. No state migration
is required solely for this directory rename.

## Configuration

Both roots pin Terraform and the AWS provider. Keep `.terraform.lock.hcl` files in
Git. Backend configuration, actual `.tfvars`, state files, saved plans and downloaded
providers are ignored. The runtime root includes `backend.s3.hcl.example` and
`pilot.tfvars.example`; they contain placeholders, not deployment credentials.

For an existing deployment, use its established backend and inputs. Do not recreate
existing resources from empty state. Review a fresh saved plan before applying any
change, especially after modifying source or moving directories.

GPU capacity defaults to zero and resource creation requires explicit approval.
Other provisioned resources still cost money when GPU capacity is zero. The state
bucket has deletion protection; application teardown does not target this root.
See [bootstrap details](bootstrap/README.md).

## Verification

CI initializes both roots with their backends disabled for configuration validation.
The runtime safety tests use a mocked AWS provider and do not create cloud resources:

```bash
terraform -chdir=infra/runtime fmt -check -recursive
terraform -chdir=infra/runtime validate
terraform -chdir=infra/runtime test
terraform -chdir=infra/bootstrap validate
```

The offline cost calculator reads the versioned rate snapshot in `runtime/costs.json`:

```bash
uv run python -m scripts.weather_costs --hours 8 --gpu-hours 8
```

That snapshot is dated, not a current quote or a spending cap. Refresh pricing before
approving another deployment. Kubernetes assets and image definitions are in
[`deploy/`](../deploy/README.md); generated manifests and local `docs/` are not published.
