locals {
  stream_name = "${var.name_prefix}-kinesis"
}

resource "aws_kinesis_stream" "this" {
  name             = local.stream_name
  shard_count      = var.shard_count
  retention_period = var.retention_hours

  stream_mode_details {
    stream_mode = "PROVISIONED"
  }

  tags = var.tags
}
