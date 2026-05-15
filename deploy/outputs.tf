output "ecr_repository_url" {
  description = "Push images here."
  value       = aws_ecr_repository.topography.repository_url
}

output "lambda_function_name" {
  value = aws_lambda_function.topography.function_name
}

output "lambda_function_arn" {
  value = aws_lambda_function.topography.arn
}

output "lambda_role_arn" {
  value = aws_iam_role.lambda_exec.arn
}

output "sqs_queue_url" {
  description = "Set as AWS_LAMBDA_TOPOGRAPHY_QUEUE_URL (or equivalent) in the Django backend."
  value       = aws_sqs_queue.jobs.url
}

output "sqs_queue_arn" {
  value = aws_sqs_queue.jobs.arn
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.lambda.name
}
