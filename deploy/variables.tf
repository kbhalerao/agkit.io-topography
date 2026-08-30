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

variable "sqs_max_receive_count" {
  description = "Deliveries before a message is parked on the DLQ. One retry: a message carries a whole bundle (five jobs by default) at up to 900s each, and a redelivery re-runs the siblings that already succeeded."
  type        = number
  default     = 2
}

variable "sqs_visibility_timeout_seconds" {
  description = "Must be >= lambda_timeout_seconds. AWS recommendation: 6x the function timeout, but we cap to the max useful value."
  type        = number
  default     = 960
}

# --------------------------------------------------------------------------
# Sync elevation HTTP endpoint (deploy/sync_http.tf)
# --------------------------------------------------------------------------
variable "sync_lambda_memory_mb" {
  description = "Memory for the sync HTTP Lambda. More memory = more vCPU = faster GDAL cold init + render."
  type        = number
  default     = 3008
}

variable "sync_lambda_timeout_seconds" {
  description = "Timeout for the sync HTTP Lambda. Capped at 30s by the API Gateway integration."
  type        = number
  default     = 30
}

variable "metering_enabled" {
  description = "Turn on x402 self-metering in the sync Lambda. When false, the endpoint serves unmetered (good for initial smoke testing)."
  type        = bool
  default     = false
}

variable "x402_base_url" {
  description = "x402 control-plane base URL (e.g. https://x402.agkit.io). Required when metering_enabled."
  type        = string
  default     = ""
}

variable "x402_gateway_token" {
  description = "DRF token for the x402 topo-gateway user (from create_gateway_user). Required when metering_enabled. Consider Secrets Manager for hardening."
  type        = string
  default     = ""
  sensitive   = true
}

variable "metering_catalog_path_prefix" {
  description = "Namespaces the request path to the catalog item: request /elevation + this prefix = /x402/v1/topo/elevation."
  type        = string
  default     = "/x402/v1/topo"
}

variable "custom_domain_name" {
  description = "Custom domain for the endpoint, e.g. topo.agkit.io. Leave empty to use the execute-api URL. Requires certificate_arn."
  type        = string
  default     = ""
}

variable "certificate_arn" {
  description = "ARN of a validated ACM cert (in aws_region) for custom_domain_name. DNS is in Cloudflare, so validate the cert out of band and pass its ARN here."
  type        = string
  default     = ""
}
