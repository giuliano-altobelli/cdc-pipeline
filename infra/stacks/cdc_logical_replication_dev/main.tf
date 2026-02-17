data "aws_vpc" "default" {
  count   = var.use_default_vpc ? 1 : 0
  default = true
}

data "aws_subnets" "default" {
  count = var.use_default_vpc ? 1 : 0

  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }
}

locals {
  selected_vpc_id   = var.use_default_vpc ? data.aws_vpc.default[0].id : var.vpc_id
  selected_subnets  = var.use_default_vpc ? data.aws_subnets.default[0].ids : var.subnet_ids
  bootstrap_command = "${path.module}/bootstrap_db.sh"
}

module "rds_postgres_logical_replication" {
  source = "../../modules/rds_postgres_logical_replication"

  name_prefix             = var.name_prefix
  tags                    = var.tags
  vpc_id                  = local.selected_vpc_id
  subnet_ids              = local.selected_subnets
  allowed_cidrs           = var.allowed_cidrs
  db_instance_class       = var.db_instance_class
  db_engine_version       = var.db_engine_version
  db_allocated_storage_gb = var.db_allocated_storage_gb
  db_name                 = var.db_name
  db_master_username      = var.db_master_username
  replication_username    = var.replication_username
}

module "kinesis_stream" {
  source = "../../modules/kinesis_stream"

  name_prefix     = var.name_prefix
  shard_count     = var.kinesis_shard_count
  retention_hours = var.kinesis_retention_hours
  tags            = var.tags
}

resource "null_resource" "bootstrap_db" {
  triggers = {
    db_instance_id = module.rds_postgres_logical_replication.db_instance_id
    db_resource_id = module.rds_postgres_logical_replication.db_resource_id
    db_endpoint    = module.rds_postgres_logical_replication.host
    db_port        = tostring(module.rds_postgres_logical_replication.port)
    db_name        = module.rds_postgres_logical_replication.database
    db_user        = module.rds_postgres_logical_replication.master_username
    repl_user      = module.rds_postgres_logical_replication.replication_username
    script_sha     = filesha256("${path.module}/bootstrap_db.sh")
  }

  provisioner "local-exec" {
    command = local.bootstrap_command
    environment = {
      PGHOST               = module.rds_postgres_logical_replication.host
      PGPORT               = tostring(module.rds_postgres_logical_replication.port)
      PGDATABASE           = module.rds_postgres_logical_replication.database
      PGUSER               = module.rds_postgres_logical_replication.master_username
      PGPASSWORD           = module.rds_postgres_logical_replication.master_password
      REPLICATION_USERNAME = module.rds_postgres_logical_replication.replication_username
      REPLICATION_PASSWORD = module.rds_postgres_logical_replication.replication_password
    }
  }

  depends_on = [module.rds_postgres_logical_replication]
}
