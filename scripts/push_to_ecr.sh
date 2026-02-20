#!/usr/bin/env bash
set -euo pipefail

required_env_vars=(
  "AWS_REGION"
  "AWS_ACCOUNT_ID"
  "ECR_REPOSITORY"
)

for env_var in "${required_env_vars[@]}"; do
  if [[ -z "${!env_var:-}" ]]; then
    echo "Missing required environment variable: ${env_var}" >&2
    exit 1
  fi
done

git_sha="${IMAGE_TAG:-$(git rev-parse --short=12 HEAD)}"
local_image="cdc-logical-replication:${git_sha}"
registry="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
remote_image="${registry}/${ECR_REPOSITORY}"

echo "Building ${local_image}"
docker build -t "${local_image}" .
docker tag "${local_image}" "cdc-logical-replication:latest"

echo "Logging in to ${registry}"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${registry}"

echo "Tagging images for ECR"
docker tag "${local_image}" "${remote_image}:${git_sha}"
docker tag "${local_image}" "${remote_image}:latest"

echo "Pushing ${remote_image}:${git_sha}"
docker push "${remote_image}:${git_sha}"

echo "Pushing ${remote_image}:latest"
docker push "${remote_image}:latest"

echo "Push complete:"
echo "  ${remote_image}:${git_sha}"
echo "  ${remote_image}:latest"
