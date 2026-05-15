#!/usr/bin/env bash
# deploy/deploy.sh — Build, push, and deploy the topography Lambda.
#
# Subcommands:
#   plan        Show terraform plan (no changes applied).
#   build-push  Build the Docker image and push to ECR (no TF changes).
#   deploy      Default. Build + push + terraform apply.
#   destroy     Tear down ALL resources managed by this stack. Prompts first.
#   outputs     Print terraform outputs (Lambda ARN, queue URL, etc.).
#
# Environment:
#   AWS_REGION   Defaults to us-west-2 (colocated with USGS DEM bucket).
#   IMAGE_TAG    Defaults to current git short SHA.
#
# Uses your default AWS profile. State is local to deploy/ and gitignored.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TF_DIR="${SCRIPT_DIR}"

AWS_REGION="${AWS_REGION:-us-west-2}"
IMAGE_NAME="${IMAGE_NAME:-agkit-topography}"

GIT_SHA="$(cd "${REPO_ROOT}" && git rev-parse --short HEAD 2>/dev/null || echo latest)"
IMAGE_TAG="${IMAGE_TAG:-${GIT_SHA}}"

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' is required but not on PATH" >&2; exit 1; }
}
require aws
require docker
require terraform
require git

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
ECR_REPO_URL="${ECR_REGISTRY}/${IMAGE_NAME}"

TF_VARS=(-var "image_tag=${IMAGE_TAG}" -var "aws_region=${AWS_REGION}")

tf_init() {
  cd "${TF_DIR}"
  terraform init -input=false
}

# Ensure the ECR repo exists before we attempt to push to it. Idempotent;
# no-op once the resource is in the TF state.
ensure_ecr() {
  cd "${TF_DIR}"
  echo "==> Ensuring ECR repository exists"
  terraform apply -auto-approve \
    -target=aws_ecr_repository.topography \
    -target=aws_ecr_lifecycle_policy.topography \
    "${TF_VARS[@]}" >/dev/null
}

build_push() {
  echo "==> Building image ${IMAGE_NAME}:${IMAGE_TAG}"
  cd "${REPO_ROOT}"
  docker build -t "${IMAGE_NAME}:${IMAGE_TAG}" .
  docker tag "${IMAGE_NAME}:${IMAGE_TAG}" "${ECR_REPO_URL}:${IMAGE_TAG}"
  docker tag "${IMAGE_NAME}:${IMAGE_TAG}" "${ECR_REPO_URL}:latest"

  echo "==> Logging in to ${ECR_REGISTRY}"
  aws ecr get-login-password --region "${AWS_REGION}" \
    | docker login --username AWS --password-stdin "${ECR_REGISTRY}"

  echo "==> Pushing ${ECR_REPO_URL}:${IMAGE_TAG}"
  docker push "${ECR_REPO_URL}:${IMAGE_TAG}"
  docker push "${ECR_REPO_URL}:latest"
}

tf_plan() {
  cd "${TF_DIR}"
  terraform plan "${TF_VARS[@]}"
}

tf_apply() {
  cd "${TF_DIR}"
  terraform apply "${TF_VARS[@]}"
}

tf_destroy() {
  cd "${TF_DIR}"
  echo "About to destroy ALL resources managed by this stack:"
  terraform state list || true
  echo
  read -r -p "Type 'destroy' to continue: " confirm
  [[ "${confirm}" == "destroy" ]] || { echo "Aborted."; exit 1; }
  terraform destroy "${TF_VARS[@]}"
}

tf_outputs() {
  cd "${TF_DIR}"
  terraform output
}

cmd="${1:-deploy}"

case "${cmd}" in
  plan)
    tf_init
    tf_plan
    ;;
  build-push)
    tf_init
    ensure_ecr
    build_push
    ;;
  deploy)
    tf_init
    ensure_ecr
    build_push
    tf_apply
    ;;
  destroy)
    tf_init
    tf_destroy
    ;;
  outputs)
    tf_init
    tf_outputs
    ;;
  *)
    echo "Unknown subcommand: ${cmd}" >&2
    echo "Usage: $0 [plan|build-push|deploy|destroy|outputs]" >&2
    exit 1
    ;;
esac
