data "aws_caller_identity" "current" {}

# --------------------------------------------------------------------------
# ECR — image registry for the Lambda container.
# --------------------------------------------------------------------------
resource "aws_ecr_repository" "topography" {
  name                 = var.name_prefix
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "topography" {
  repository = aws_ecr_repository.topography.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep only the 10 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# --------------------------------------------------------------------------
# SQS — job queue. Django pushes a JSON list of jobs as a single message.
# --------------------------------------------------------------------------
resource "aws_sqs_queue" "jobs" {
  name                       = "${var.name_prefix}-jobs"
  visibility_timeout_seconds = var.sqs_visibility_timeout_seconds
  message_retention_seconds  = 345600 # 4 days
  receive_wait_time_seconds  = 20     # long polling
}

# --------------------------------------------------------------------------
# IAM — Lambda execution role.
# Only what the function needs: write its own logs, drain its own queue.
# No S3 write (postbacks go over HTTPS), no static AWS keys.
# --------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name               = "${var.name_prefix}-lambda-exec"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "lambda_sqs" {
  statement {
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }
}

resource "aws_iam_role_policy" "lambda_sqs" {
  name   = "${var.name_prefix}-sqs"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_sqs.json
}

# --------------------------------------------------------------------------
# CloudWatch Logs — declared explicitly so retention is bounded.
# Without this, Lambda auto-creates the group with infinite retention.
# --------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name_prefix}"
  retention_in_days = var.log_retention_days
}

# --------------------------------------------------------------------------
# Lambda — image-based function. Entry point baked into the image
# (ENTRYPOINT/CMD in Dockerfile: app.handler.handler).
# --------------------------------------------------------------------------
resource "aws_lambda_function" "topography" {
  function_name = var.name_prefix
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.topography.repository_url}:${var.image_tag}"
  architectures = ["x86_64"]

  memory_size = var.lambda_memory_mb
  timeout     = var.lambda_timeout_seconds

  ephemeral_storage {
    size = var.lambda_ephemeral_storage_mb
  }

  reserved_concurrent_executions = var.lambda_reserved_concurrency

  environment {
    variables = {
      POSTBACK_TIMEOUT_SECONDS = "60"
      IN_TEST                  = "false"
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_basic,
    aws_iam_role_policy.lambda_sqs,
    aws_cloudwatch_log_group.lambda,
  ]
}

resource "aws_lambda_event_source_mapping" "sqs" {
  event_source_arn                   = aws_sqs_queue.jobs.arn
  function_name                      = aws_lambda_function.topography.arn
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]
}
