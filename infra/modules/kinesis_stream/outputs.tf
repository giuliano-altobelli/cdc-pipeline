output "stream_name" {
  description = "Kinesis stream name."
  value       = aws_kinesis_stream.this.name
}

output "stream_arn" {
  description = "Kinesis stream ARN."
  value       = aws_kinesis_stream.this.arn
}
