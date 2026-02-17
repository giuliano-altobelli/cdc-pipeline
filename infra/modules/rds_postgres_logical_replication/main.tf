locals {
  sanitized_prefix       = trimsuffix(replace(lower(var.name_prefix), "_", "-"), "-")
  identifier_prefix      = substr(local.sanitized_prefix, 0, 52)
  db_identifier          = "${local.identifier_prefix}-pg"
  parameter_group_family = "postgres${regex("^[0-9]+", var.db_engine_version)}"
}

resource "random_password" "master" {
  length           = 32
  special          = true
  override_special = "!#%^*()-_=+[]{}<>?"
}

resource "random_password" "replication" {
  length           = 32
  special          = true
  override_special = "!#%^*()-_=+[]{}<>?"
}

resource "aws_db_subnet_group" "this" {
  name       = "${local.identifier_prefix}-subnet"
  subnet_ids = var.subnet_ids

  tags = merge(var.tags, {
    Name = "${local.db_identifier}-subnet"
  })
}

resource "aws_security_group" "postgres" {
  name        = "${local.identifier_prefix}-pg-sg"
  description = "Postgres access for ${local.db_identifier}"
  vpc_id      = var.vpc_id

  ingress {
    description = "Postgres ingress"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name = "${local.db_identifier}-sg"
  })
}

resource "aws_db_parameter_group" "logical_replication" {
  name        = "${local.identifier_prefix}-pg-params"
  family      = local.parameter_group_family
  description = "Logical replication settings for ${local.db_identifier}"

  parameter {
    name         = "rds.logical_replication"
    value        = "1"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "max_replication_slots"
    value        = "10"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "max_wal_senders"
    value        = "10"
    apply_method = "pending-reboot"
  }

  tags = merge(var.tags, {
    Name = "${local.db_identifier}-params"
  })
}

resource "aws_db_instance" "this" {
  identifier = local.db_identifier

  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage_gb
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_master_username
  password = random_password.master.result
  port     = 5432

  publicly_accessible = true
  multi_az            = false

  backup_retention_period = 0
  skip_final_snapshot     = true
  deletion_protection     = false
  apply_immediately       = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.postgres.id]
  parameter_group_name   = aws_db_parameter_group.logical_replication.name

  tags = merge(var.tags, {
    Name = local.db_identifier
  })
}
