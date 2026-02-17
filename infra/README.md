# CDC Logical Replication Dev Infrastructure

This directory contains Terraform code for Phase 1 of CDC logical replication integration testing.

## Structure
- `modules/kinesis_stream`: reusable Kinesis Data Stream module
- `modules/rds_postgres_logical_replication`: reusable RDS Postgres module with logical replication settings
- `stacks/cdc_logical_replication_dev`: developer stack wiring modules together and bootstrapping DB roles

## Authentication
Terraform uses standard AWS environment variables:
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_SESSION_TOKEN` (optional)
- `AWS_REGION` (optional override; stack defaults to `us-west-2`)

## State And Lock Files
This repository ignores local Terraform state and lock artifacts under `services/infra/` for developer-only workflows.
If you later need reproducible provider locks, remove the `.terraform.lock.hcl` ignore rule and commit lock files.
