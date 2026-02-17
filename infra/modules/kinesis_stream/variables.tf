variable "name_prefix" {
  description = "Prefix used to construct stream names."
  type        = string
}

variable "shard_count" {
  description = "Number of shards for the stream."
  type        = number
  default     = 1

  validation {
    condition     = var.shard_count >= 1
    error_message = "shard_count must be at least 1."
  }
}

variable "retention_hours" {
  description = "Retention period in hours."
  type        = number
  default     = 24

  validation {
    condition     = var.retention_hours >= 24 && var.retention_hours <= 8760
    error_message = "retention_hours must be between 24 and 8760."
  }
}

variable "tags" {
  description = "Tags applied to the stream."
  type        = map(string)
  default     = {}
}
