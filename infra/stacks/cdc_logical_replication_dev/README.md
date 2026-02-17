# CDC Logical Replication Dev Stack

This stack provisions:
- a public RDS PostgreSQL instance configured for logical replication (wal2json compatible settings)
- a Kinesis Data Stream for CDC event publishing
- a local bootstrap step to create and grant a dedicated replication user

## Prerequisites
- Terraform `>= 1.5`
- AWS credentials exported in your shell
- One of:
  - Docker (preferred, used to run `psql` in `postgres:15-alpine`)
  - local `psql` binary on your `PATH`

## Configure
Create `terraform.tfvars` in this directory:

```hcl
allowed_cidrs = ["203.0.113.10/32"]
# Optional:
# aws_region = "us-west-2"
# name_prefix = "cdc-lr-dev"
# use_default_vpc = true
```

If `use_default_vpc = false`, also provide:
- `vpc_id`
- `subnet_ids` (at least 2)

## Apply
```bash
terraform init
terraform apply
```

The apply includes a `null_resource` local bootstrap that:
- connects with `sslmode=require`
- creates/updates the replication role/password idempotently
- grants `rds_replication` when available
- grants `CONNECT` on the target database

## Useful Outputs
```bash
terraform output env_exports
terraform output -raw pg_master_password
terraform output -raw pg_replication_password
```

## Notes
- `.terraform.lock.hcl` is currently ignored in this repository for this local developer workflow.
- Consider tightening `allowed_cidrs` to your exact public IP `/32`.
