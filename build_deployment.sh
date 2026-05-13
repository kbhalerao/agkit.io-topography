#!/bin/bash
# Build the topography Lambda image.
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-agkit-topography}"
TAG="${TAG:-latest}"

docker build -t "${IMAGE_NAME}:${TAG}" .
echo "Built ${IMAGE_NAME}:${TAG}"
