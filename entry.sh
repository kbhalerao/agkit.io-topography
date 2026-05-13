#!/bin/sh
# Lambda Runtime Interface Client bootstrap.
# When AWS_LAMBDA_RUNTIME_API is unset (local), wrap with the RIE.
if [ -z "${AWS_LAMBDA_RUNTIME_API}" ]; then
    exec /usr/bin/aws-lambda-rie python3 -m awslambdaric "$1"
else
    exec python3 -m awslambdaric "$1"
fi
