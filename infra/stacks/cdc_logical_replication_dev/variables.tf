variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-west-2"
}

variable "name_prefix" {
  description = "Prefix used for resource names."
  type        = string
  default     = "cdc-lr-dev"
}

variable "tags" {
  description = "Tags applied to all resources."
  type        = map(string)
  default = {
    project   = "data-platform"
    component = "cdc-logical-replication"
    env       = "dev"
  }
}

variable "allowed_cidrs" {
  description = "CIDR blocks that can reach Postgres on port 5432."
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
  description = "Pinned Postgres engine version."
  type        = string
  default     = "15.12"
}

variable "db_allocated_storage_gb" {
  description = "Allocated storage in GB."
  type        = number
  default     = 20
}

variable "db_name" {
  description = "Initial database name."
  type        = string
  default     = "cdc"
}

variable "db_master_username" {
  description = "Master DB username."
  type        = string
  default     = "cdc_admin"
}

variable "replication_username" {
  description = "Replication DB username."
  type        = string
  default     = "cdc_replication"
}

variable "kinesis_shard_count" {
  description = "Kinesis stream shard count."
  type        = number
  default     = 1
}

variable "kinesis_retention_hours" {
  description = "Kinesis stream retention in hours."
  type        = number
  default     = 24
}

variable "use_default_vpc" {
  description = "Use the account's default VPC and all its subnets."
  type        = bool
  default     = true
}

variable "vpc_id" {
  description = "Custom VPC ID when use_default_vpc is false."
  type        = string
  default     = null

  validation {
    condition     = var.use_default_vpc || (var.vpc_id != null && trim(var.vpc_id) != "")
    error_message = "vpc_id is required when use_default_vpc is false."
  }
}

variable "subnet_ids" {
  description = "Custom subnet IDs when use_default_vpc is false."
  type        = list(string)
  default     = []

  validation {
    condition     = var.use_default_vpc || length(var.subnet_ids) >= 2
    error_message = "subnet_ids must include at least two subnets when use_default_vpc is false."
  }
}
