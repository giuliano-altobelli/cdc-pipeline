#!/usr/bin/env bash
set -euo pipefail

required_vars=(
  PGHOST
  PGPORT
  PGDATABASE
  PGUSER
  PGPASSWORD
  REPLICATION_USERNAME
  REPLICATION_PASSWORD
)

for required_var in "${required_vars[@]}"; do
  if [[ -z "${!required_var:-}" ]]; then
    echo "Missing required environment variable: ${required_var}" >&2
    exit 1
  fi
done

conninfo="host=${PGHOST} port=${PGPORT} dbname=${PGDATABASE} user=${PGUSER} sslmode=require"

run_psql() {
  if command -v docker >/dev/null 2>&1; then
    if docker run --rm -i \
      -e PGPASSWORD="${PGPASSWORD}" \
      postgres:15-alpine \
      psql "$@"; then
      return
    fi

    if command -v psql >/dev/null 2>&1; then
      echo "Dockerized psql failed; falling back to local psql." >&2
      PGPASSWORD="${PGPASSWORD}" psql "$@"
      return
    fi

    return 1
  fi

  if command -v psql >/dev/null 2>&1; then
    PGPASSWORD="${PGPASSWORD}" psql "$@"
    return
  fi

  echo "Neither docker nor psql is installed; bootstrap requires one of them." >&2
  return 1
}

bootstrap_sql=$(cat <<'SQL'
SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L',
  :'replication_username',
  :'replication_password'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = :'replication_username'
) \gexec

SELECT format(
  'ALTER ROLE %I LOGIN PASSWORD %L',
  :'replication_username',
  :'replication_password'
)
WHERE EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = :'replication_username'
) \gexec

SELECT format(
  'ALTER ROLE %I REPLICATION',
  :'replication_username'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = 'rds_replication'
) \gexec

SELECT format(
  'GRANT rds_replication TO %I',
  :'replication_username'
)
WHERE EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = 'rds_replication'
) \gexec

SELECT format(
  'GRANT CONNECT ON DATABASE %I TO %I',
  :'target_database',
  :'replication_username'
) \gexec
SQL
)

for attempt in $(seq 1 30); do
  if run_psql \
    -v ON_ERROR_STOP=1 \
    -v replication_username="${REPLICATION_USERNAME}" \
    -v replication_password="${REPLICATION_PASSWORD}" \
    -v target_database="${PGDATABASE}" \
    -f - \
    "${conninfo}" <<<"${bootstrap_sql}"; then
    echo "Database bootstrap complete."
    exit 0
  fi

  echo "Bootstrap attempt ${attempt}/30 failed; retrying in 10 seconds..." >&2
  sleep 10
done

echo "Bootstrap failed after 30 attempts." >&2
exit 1
