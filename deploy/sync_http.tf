# ============================================================================
# Sync elevation HTTP endpoint.
#
# A SECOND Lambda function on the SAME container image as the SQS worker, with
# its CMD overridden to the HTTP handler (app.http_handler.handler). Fronted by
# an API Gateway HTTP API (POST /elevation) and, optionally, a custom domain
# (topo.agkit.io) whose DNS lives in Cloudflare. The function self-meters
# against x402 (app/metering.py) — no gateway in front does the billing.
#
# Cold starts (~2-5s on the GDAL image) are accepted; no provisioned
# concurrency. To add it later: publish a version/alias and an
# aws_lambda_provisioned_concurrency_config.
# ============================================================================

resource "aws_cloudwatch_log_group" "sync_http" {
  name              = "/aws/lambda/${var.name_prefix}-sync"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "sync_http" {
  function_name = "${var.name_prefix}-sync"
  role          = aws_iam_role.lambda_exec.arn # reuse: only needs CloudWatch logs
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.topography.repository_url}:${var.image_tag}"
  architectures = ["x86_64"]

  # Same image, different entry point: override the Dockerfile CMD
  # (app.handler.handler → the SQS worker) with the HTTP handler.
  image_config {
    command = ["app.http_handler.handler"]
  }

  memory_size = var.sync_lambda_memory_mb
  timeout     = var.sync_lambda_timeout_seconds

  environment {
    variables = {
      IN_TEST                      = "false"
      METERING_ENABLED             = tostring(var.metering_enabled)
      X402_BASE_URL                = var.x402_base_url
      X402_GATEWAY_TOKEN           = var.x402_gateway_token
      METERING_CATALOG_PATH_PREFIX = var.metering_catalog_path_prefix
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_basic,
    aws_cloudwatch_log_group.sync_http,
  ]
}

# --------------------------------------------------------------------------
# API Gateway HTTP API — POST /elevation → the sync Lambda.
# --------------------------------------------------------------------------
resource "aws_apigatewayv2_api" "sync_http" {
  name          = "${var.name_prefix}-sync"
  protocol_type = "HTTP"
  description   = "Sync USGS elevation: boundary in, PNG out."
}

resource "aws_apigatewayv2_integration" "sync_http" {
  api_id                 = aws_apigatewayv2_api.sync_http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.sync_http.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
  # HTTP API caps integration timeout at 30s; keep it ≤ the function timeout.
  timeout_milliseconds = min(var.sync_lambda_timeout_seconds, 30) * 1000
}

# POST does the work; OPTIONS reaches the handler too, which answers the CORS
# preflight itself (keeps all CORS logic in app/http_handler.py).
resource "aws_apigatewayv2_route" "post_elevation" {
  api_id    = aws_apigatewayv2_api.sync_http.id
  route_key = "POST /elevation"
  target    = "integrations/${aws_apigatewayv2_integration.sync_http.id}"
}

resource "aws_apigatewayv2_route" "options_elevation" {
  api_id    = aws_apigatewayv2_api.sync_http.id
  route_key = "OPTIONS /elevation"
  target    = "integrations/${aws_apigatewayv2_integration.sync_http.id}"
}

resource "aws_cloudwatch_log_group" "sync_http_access" {
  name              = "/aws/apigateway/${var.name_prefix}-sync"
  retention_in_days = var.log_retention_days
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.sync_http.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.sync_http_access.arn
    format = jsonencode({
      requestId         = "$context.requestId"
      httpMethod        = "$context.httpMethod"
      path              = "$context.path"
      status            = "$context.status"
      responseLat       = "$context.responseLatency"
      integrationStatus = "$context.integration.status"
    })
  }
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.sync_http.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.sync_http.execution_arn}/*/*"
}

# --------------------------------------------------------------------------
# Custom domain (topo.agkit.io) — OPTIONAL.
#
# Created only when both custom_domain_name and certificate_arn are set. DNS is
# in Cloudflare, so the ACM cert (in this region) must be created + DNS-validated
# out of band (Cloudflare CNAME for the validation record), then its ARN passed
# in. Once applied, CNAME the domain in Cloudflare to the output
# `sync_http_custom_domain_target`.
# --------------------------------------------------------------------------
locals {
  enable_custom_domain = var.custom_domain_name != "" && var.certificate_arn != ""
}

resource "aws_apigatewayv2_domain_name" "sync_http" {
  count       = local.enable_custom_domain ? 1 : 0
  domain_name = var.custom_domain_name

  domain_name_configuration {
    certificate_arn = var.certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }
}

resource "aws_apigatewayv2_api_mapping" "sync_http" {
  count       = local.enable_custom_domain ? 1 : 0
  api_id      = aws_apigatewayv2_api.sync_http.id
  domain_name = aws_apigatewayv2_domain_name.sync_http[0].id
  stage       = aws_apigatewayv2_stage.default.id
}
