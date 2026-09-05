# Weather AWS Bootstrap

This separate Terraform root owns only the protected state bucket and the dedicated
EKS operator role. It does not grant that role AWS administrator permissions. The
role trusts one explicit same-account IAM principal and can describe only the weather
cluster; cluster administration is granted by the main stack's EKS access entry.

The current account was bootstrapped on September 5, 2026. Its initial local state
was migrated into the private, encrypted, versioned bucket at
`weather/bootstrap.tfstate`. The main stack uses `weather/pilot.tfstate` in the same
bucket. Both backends use S3 lockfiles. Do not delete the state bucket during runtime
teardown: it has Terraform `prevent_destroy` and `force_destroy = false`.

Account-specific inputs and backend configuration are ignored by Git. For an
existing deployment, initialize this root with its `backend.s3.hcl`, then plan with
`-var-file=pilot.tfvars`. Review a saved plan before applying it. Do not try to
recreate the bootstrap bucket with an empty state.

For a different account, the bucket must first be created with durable local
bootstrap state, then this root's state migrated to S3 after verifying encryption,
versioning, public-access blocking and TLS-only policy. The checked-in S3 backend
intentionally cannot initialize against a bucket that does not exist.
