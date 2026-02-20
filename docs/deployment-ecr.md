# Deploying `cdc-logical-replication` via AWS ECR

This document describes how to build, tag, and push the service image to an existing ECR repository.

## Prerequisites

- Docker installed and running
- AWS CLI v2 configured with credentials allowed to push to ECR
- Existing ECR repository

## Required variables

Set these before building and pushing:

```bash
export AWS_REGION=us-west-2
export AWS_ACCOUNT_ID=123456789012
export ECR_REPOSITORY=cdc-logical-replication
```

Derive image identifiers:

```bash
export GIT_SHA="$(git rev-parse --short=12 HEAD)"
export ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
export ECR_IMAGE="${ECR_REGISTRY}/${ECR_REPOSITORY}"
```

## Build the image

```bash
docker build -t cdc-logical-replication:${GIT_SHA} .
docker tag cdc-logical-replication:${GIT_SHA} cdc-logical-replication:latest
```

## Authenticate Docker to ECR

```bash
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${ECR_REGISTRY}"
```

## Tag for ECR and push

```bash
docker tag cdc-logical-replication:${GIT_SHA} "${ECR_IMAGE}:${GIT_SHA}"
docker tag cdc-logical-replication:${GIT_SHA} "${ECR_IMAGE}:latest"

docker push "${ECR_IMAGE}:${GIT_SHA}"
docker push "${ECR_IMAGE}:latest"
```

## Optional helper script

Use `scripts/push_to_ecr.sh` to run the full sequence:

```bash
./scripts/push_to_ecr.sh
```

## Runtime configuration

At runtime, provide all required application variables:

- `PGHOST`
- `PGUSER`
- `PGPASSWORD`
- `PGDATABASE`
- `AWS_REGION`
- `KINESIS_STREAM`

Common optional variables:

- `PGPORT` (default `5432`)
- `REPLICATION_SLOT` (default `etl_slot_wal2json`)
- `LOG_LEVEL` (default `INFO`)
- Kinesis batching/retry tuning variables from `src/cdc_logical_replication/settings.py`

## Pull and run example

```bash
docker pull "${ECR_IMAGE}:${GIT_SHA}"

docker run --rm \
  -e PGHOST \
  -e PGUSER \
  -e PGPASSWORD \
  -e PGDATABASE \
  -e AWS_REGION \
  -e KINESIS_STREAM \
  "${ECR_IMAGE}:${GIT_SHA}"
```

## Troubleshooting

- `no basic auth credentials`: run the ECR login command again for the correct region/account.
- `repository does not exist`: create it first with `aws ecr create-repository`.
- startup `ValidationError` from settings: required environment variables are missing or invalid.
