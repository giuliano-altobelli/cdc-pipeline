output "host" {
  description = "RDS endpoint hostname."
  value       = aws_db_instance.this.address
}

output "port" {
  description = "RDS endpoint port."
  value       = aws_db_instance.this.port
}

output "database" {
  description = "Database name."
  value       = var.db_name
}

output "master_username" {
  description = "Master DB username."
  value       = var.db_master_username
}

output "master_password" {
  description = "Master DB password."
  value       = random_password.master.result
  sensitive   = true
}

output "replication_username" {
  description = "Replication DB username."
  value       = var.replication_username
}

output "replication_password" {
  description = "Replication DB password."
  value       = random_password.replication.result
  sensitive   = true
}

output "db_instance_id" {
  description = "RDS instance identifier."
  value       = aws_db_instance.this.id
}

output "db_resource_id" {
  description = "RDS immutable resource identifier."
  value       = aws_db_instance.this.resource_id
}
