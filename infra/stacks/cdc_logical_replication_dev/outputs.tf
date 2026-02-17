output "pg_host" {
  description = "Postgres host."
  value       = module.rds_postgres_logical_replication.host
}

output "pg_port" {
  description = "Postgres port."
  value       = module.rds_postgres_logical_replication.port
}

output "pg_database" {
  description = "Postgres database name."
  value       = module.rds_postgres_logical_replication.database
}

output "pg_master_user" {
  description = "Master Postgres user."
  value       = module.rds_postgres_logical_replication.master_username
}

output "pg_master_password" {
  description = "Master Postgres password."
  value       = module.rds_postgres_logical_replication.master_password
  sensitive   = true
}

output "pg_replication_user" {
  description = "Replication Postgres user."
  value       = module.rds_postgres_logical_replication.replication_username
}

output "pg_replication_password" {
  description = "Replication Postgres password."
  value       = module.rds_postgres_logical_replication.replication_password
  sensitive   = true
}

output "kinesis_stream_name" {
  description = "Kinesis stream name."
  value       = module.kinesis_stream.stream_name
}

output "kinesis_stream_arn" {
  description = "Kinesis stream ARN."
  value       = module.kinesis_stream.stream_arn
}

output "aws_region" {
  description = "AWS region in use."
  value       = var.aws_region
}

output "env_exports" {
  description = "Non-sensitive values and env var names for local runs."
  value = {
    PGHOST         = module.rds_postgres_logical_replication.host
    PGPORT         = tostring(module.rds_postgres_logical_replication.port)
    PGUSER         = module.rds_postgres_logical_replication.master_username
    PGPASSWORD     = "<set-from-pg_master_password-output>"
    PGDATABASE     = module.rds_postgres_logical_replication.database
    AWS_REGION     = var.aws_region
    KINESIS_STREAM = module.kinesis_stream.stream_name
  }
}
