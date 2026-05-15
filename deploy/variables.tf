variable "aws_region" {
  description = "AWS region. Default us-west-2 colocates with USGS DEM bucket prd-tnm for free, low-latency reads."
  type        = string
  default     = "us-west-2"
}

variable "name_prefix" {
  description = "Prefix for all AWS resource names. Change only if it collides with existing infra in the account."
  type        = string
  default     = "agkit-topography"
}

variable "image_tag" {
  description = "ECR image tag to deploy. Set by deploy.sh from the current git short SHA."
  type        = string
}

variable "lambda_memory_mb" {
  description = "Lambda memory. GRASS r.watershed is the bottleneck; more memory also gives more vCPU."
  type        = number
  default     = 4096
}

variable "lambda_timeout_seconds" {
  description = "Lambda timeout. Max is 900 (15 min)."
  type        = number
  default     = 900
}

variable "lambda_ephemeral_storage_mb" {
  description = "Size of /tmp. DEM tiles + GRASS mapset live here."
  type        = number
  default     = 4096
}

variable "lambda_reserved_concurrency" {
  description = "Cap on concurrent invocations. Prevents a burst from overwhelming the Django postback endpoint."
  type        = number
  default     = 10
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda log group."
  type        = number
  default     = 30
}

variable "sqs_visibility_timeout_seconds" {
  description = "Must be >= lambda_timeout_seconds. AWS recommendation: 6x the function timeout, but we cap to the max useful value."
  type        = number
  default     = 960
}
