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

output "sqs_dlq_url" {
  description = "Failed messages land here. Nothing watches it yet — an alarm on ApproximateNumberOfMessagesVisible is the follow-up."
  value       = aws_sqs_queue.jobs_dlq.url
}

output "sqs_dlq_arn" {
  value = aws_sqs_queue.jobs_dlq.arn
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.lambda.name
}

# --------------------------------------------------------------------------
# Sync elevation HTTP endpoint
# --------------------------------------------------------------------------
output "sync_http_function_name" {
  value = aws_lambda_function.sync_http.function_name
}

output "sync_http_api_endpoint" {
  description = "Base URL of the API Gateway HTTP API. POST to {this}/elevation."
  value       = aws_apigatewayv2_api.sync_http.api_endpoint
}

output "sync_http_custom_domain_target" {
  description = "CNAME topo.agkit.io to this in Cloudflare (null until custom_domain_name + certificate_arn are set)."
  value       = try(aws_apigatewayv2_domain_name.sync_http[0].domain_name_configuration[0].target_domain_name, null)
}
