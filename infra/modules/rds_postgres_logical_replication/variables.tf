variable "name_prefix" {
  description = "Prefix used to construct resource names."
  type        = string
}

variable "tags" {
  description = "Tags applied to created resources."
  type        = map(string)
  default     = {}
}

variable "vpc_id" {
  description = "VPC where RDS and security group will be created."
  type        = string
}

variable "subnet_ids" {
  description = "Subnet IDs for the DB subnet group."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "subnet_ids must contain at least two subnets."
  }
}

variable "allowed_cidrs" {
  description = "CIDR blocks allowed to connect to Postgres on port 5432."
  type        = list(string)

  validation {
    condition     = length(var.allowed_cidrs) > 0
    error_message = "allowed_cidrs must contain at least one CIDR."
  }
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t3.micro"
}

variable "db_engine_version" {
  description = "RDS Postgres engine version."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+", var.db_engine_version))
    error_message = "db_engine_version must begin with a numeric major version."
  }
}

variable "db_allocated_storage_gb" {
  description = "Allocated storage in GB."
  type        = number
  default     = 20

  validation {
    condition     = var.db_allocated_storage_gb >= 20
    error_message = "db_allocated_storage_gb must be at least 20."
  }
}

variable "db_name" {
  description = "Initial database name."
  type        = string
  default     = "cdc"
}

variable "db_master_username" {
  description = "Master database username."
  type        = string
  default     = "cdc_admin"
}

variable "replication_username" {
  description = "Dedicated replication username."
  type        = string
  default     = "cdc_replication"
}
