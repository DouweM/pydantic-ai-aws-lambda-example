#!/usr/bin/env bash
# Deploy the demo: a DynamoDB table, an IAM role, a durable function and a plain function.
# Safe to re-run: it creates what is missing and updates the code of what exists.
#
# Usage: ./deploy.sh                 # SDK 3.0.1 with the OTel plugin's invocation view
#        SDK_VERSION= ./deploy.sh      # the SDK version the harness resolves, no OTel plugin
#        SDK_VERSION=3.0.1 FN_SUFFIX=-sdk3 DURABLE_HANDLER=handler_telemetry.handler ./deploy.sh
#                                    # a side-by-side copy on SDK 3 with the telemetry exporter
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
NAME="${NAME:-pydantic-durable-demo}"
MODEL_ID="${MODEL_ID:-global.anthropic.claude-haiku-5-5}"
# Logfire write token: $LOGFIRE_TOKEN, else this file. With neither, no traces leave the function.
LOGFIRE_TOKEN_FILE="${LOGFIRE_TOKEN_FILE:-$HOME/.config/pydantic-demo/logfire-write-token}"
if [[ -z "${LOGFIRE_TOKEN:-}" && -r "$LOGFIRE_TOKEN_FILE" ]]; then
  LOGFIRE_TOKEN="$(tr -d '[:space:]' < "$LOGFIRE_TOKEN_FILE")"
fi
# The OTel plugin needs SDK 3. An explicitly empty SDK_VERSION lets the harness's requirement pick it.
SDK_VERSION="${SDK_VERSION-3.0.1}"
# Approvals use `RequestInput`, which needs Pydantic AI 2.68.0 and Pydantic AI Harness 0.68.0 or later.
PYDANTIC_AI_VERSION="${PYDANTIC_AI_VERSION:-2.68.0}"
HARNESS_VERSION="${HARNESS_VERSION:-0.68.0}"
# Or build them from a Git branch or commit of github.com/pydantic/pydantic-ai:
#   PYDANTIC_AI_REF=main ./deploy.sh
# Pydantic AI, its graph library and the harness are pure Python, so they are built into wheels here;
# everything else still comes from PyPI as binary wheels.
PYDANTIC_AI_REF="${PYDANTIC_AI_REF:-}"
if [[ -z "$PYDANTIC_AI_REF" && ( -z "$PYDANTIC_AI_VERSION" || -z "$HARNESS_VERSION" ) ]]; then
  echo "Set PYDANTIC_AI_VERSION and HARNESS_VERSION to a release with RequestInput, or set PYDANTIC_AI_REF." >&2
  exit 1
fi
DURABLE_HANDLER="${DURABLE_HANDLER:-handler.handler}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# Any Python with pip packages the code; the default is the README's virtual environment.
PYTHON="${PYTHON:-$HERE/.venv/bin/python}"
BUILD="$HERE/build${FN_SUFFIX:-}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ROLE="$NAME-role"
TABLE="$NAME"
FN_SUFFIX="${FN_SUFFIX:-}"
DURABLE_FN="support-agent$FN_SUFFIX"
PLAIN_FN="support-agent-plain$FN_SUFFIX"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "DynamoDB table $TABLE"
if ! aws dynamodb describe-table --table-name "$TABLE" >/dev/null 2>&1; then
  aws dynamodb create-table --table-name "$TABLE" --billing-mode PAY_PER_REQUEST \
    --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S \
    --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE >/dev/null
  aws dynamodb wait table-exists --table-name "$TABLE"
fi

say "IAM role $ROLE"
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]
  }' >/dev/null
  # Checkpoint and state APIs plus CloudWatch Logs. The CLI does not attach this for you.
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicDurableExecutionRolePolicy
  sleep 10  # Let IAM propagate before Lambda assumes the role.
fi
# Active tracing on the durable function sends segments to X-Ray. It also gives every invocation the
# durable backend's trace header, which the OTel plugin uses as the parent of the whole execution.
aws iam attach-role-policy --role-name "$ROLE" --policy-arn arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess
aws iam put-role-policy --role-name "$ROLE" --policy-name app --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\", \"Action\": [\"bedrock:InvokeModel\", \"bedrock:InvokeModelWithResponseStream\"], \"Resource\": \"*\"},
    {\"Effect\": \"Allow\", \"Action\": [\"dynamodb:PutItem\", \"dynamodb:GetItem\", \"dynamodb:UpdateItem\", \"dynamodb:Query\"],
     \"Resource\": \"arn:aws:dynamodb:$REGION:$ACCOUNT:table/$TABLE\"}
  ]
}"
ROLE_ARN="arn:aws:iam::$ACCOUNT:role/$ROLE"

say "Package (python3.13, arm64${SDK_VERSION:+, SDK $SDK_VERSION})"
rm -rf "$BUILD" && mkdir -p "$BUILD/pkg"
DEPS=("pydantic-ai-harness[aws-lambda]==$HARNESS_VERSION" "pydantic-ai-slim[bedrock]==$PYDANTIC_AI_VERSION" "logfire==5.4.0" "opentelemetry-instrumentation-botocore")
FIND_LINKS=()
if [[ -n "$PYDANTIC_AI_REF" ]]; then
  # A full clone, so the version is computed from the release tags.
  git clone --quiet --filter=blob:none https://github.com/pydantic/pydantic-ai.git "$BUILD/src"
  git -C "$BUILD/src" checkout --quiet "$PYDANTIC_AI_REF"
  for dir in pydantic_graph pydantic_ai_slim src/pydantic_ai_harness; do
    "$PYTHON" -m pip wheel --quiet --no-deps --wheel-dir "$BUILD/wheels" "$BUILD/src/$dir"
  done
  ls "$BUILD/wheels" | sed 's/^/built: /'
  FIND_LINKS=(--find-links "$BUILD/wheels")
  PYDANTIC_AI_VERSION="$(ls "$BUILD/wheels" | sed -n 's/^pydantic_ai_slim-\([^-]*\)-.*/\1/p')"
  HARNESS_VERSION="$(ls "$BUILD/wheels" | sed -n 's/^pydantic_ai_harness-\([^-]*\)-.*/\1/p')"
  DEPS=("pydantic-ai-harness[aws-lambda]==$HARNESS_VERSION" "pydantic-ai-slim[bedrock]==$PYDANTIC_AI_VERSION" "logfire==5.4.0" "opentelemetry-instrumentation-botocore")
fi
if [[ -n "$SDK_VERSION" ]]; then
  # Pin the SDK version, and add the SDK 3 telemetry packages.
  DEPS=("pydantic-ai-harness==$HARNESS_VERSION" "pydantic-ai-slim[bedrock]==$PYDANTIC_AI_VERSION" "logfire==5.4.0" "opentelemetry-instrumentation-botocore"
        "aws-durable-execution-sdk-python==$SDK_VERSION")
  [[ "$SDK_VERSION" == 3.* ]] && DEPS+=("aws-durable-execution-sdk-python-telemetry-exporter==1.0.0"
                                        "aws-durable-execution-sdk-python-otel==2.0.0")
fi
"$PYTHON" -m pip install --quiet --target "$BUILD/pkg" --platform manylinux2014_aarch64 \
  --implementation cp --python-version 3.13 --only-binary=:all: "${FIND_LINKS[@]}" "${DEPS[@]}"
cp "$HERE"/app/*.py "$BUILD/pkg/"
[[ -d "$BUILD/pkg/aws_durable_execution_sdk_python_telemetry_exporter" ]] || rm -f "$BUILD/pkg/handler_telemetry.py"
find "$BUILD/pkg" -name '__pycache__' -prune -exec rm -rf {} +
(cd "$BUILD/pkg" && zip -qr9 "$BUILD/function.zip" .)
du -h "$BUILD/function.zip" | cut -f1 | sed 's/^/zip size: /'
grep -h '^Version:' "$BUILD"/pkg/aws_durable_execution_sdk_python-*.dist-info/METADATA | sed 's/^/durable SDK /'
if [[ -d "$BUILD/pkg/aws_durable_execution_sdk_python_otel" ]]; then echo "OTel plugin: invocation view"; else echo "OTel plugin: off"; fi

# The environment goes through a private file, so the Logfire token never appears in a command line.
ENV_FILE="$BUILD/environment.json"
trap 'rm -f "$ENV_FILE"' EXIT
(umask 077; DEMO_TABLE="$TABLE" MODEL_ID="$MODEL_ID" LOGFIRE_TOKEN="${LOGFIRE_TOKEN:-}" "$PYTHON" -c '
import json, os
variables = {"DEMO_TABLE": os.environ["DEMO_TABLE"], "MODEL_ID": os.environ["MODEL_ID"]}
if os.environ["LOGFIRE_TOKEN"]:
    variables["LOGFIRE_TOKEN"] = os.environ["LOGFIRE_TOKEN"]
print(json.dumps({"Variables": variables}))' > "$ENV_FILE")
if [[ -n "${LOGFIRE_TOKEN:-}" ]]; then echo "Logfire: on (${LOGFIRE_TOKEN:0:11}...)"; else echo "Logfire: off (no \$LOGFIRE_TOKEN and no $LOGFIRE_TOKEN_FILE)"; fi

# A role created or changed seconds ago may not be usable by Lambda yet, so retry those errors only.
retry_iam() {
  for attempt in 1 2 3 4 5 6 7 8; do
    if "$@" >/dev/null 2>"$BUILD/lambda.err"; then return 0; fi
    if ! grep -q -E "cannot be assumed|does not have permissions" "$BUILD/lambda.err" || [[ $attempt -eq 8 ]]; then
      cat "$BUILD/lambda.err" >&2
      exit 1
    fi
    sleep 5
  done
}

deploy_function() {  # name handler [durable-config]; a durable function also gets active tracing
  local fn="$1" handler="$2" durable="${3:-}"
  if aws lambda get-function --function-name "$fn" >/dev/null 2>&1; then
    aws lambda update-function-code --function-name "$fn" --zip-file "fileb://$BUILD/function.zip" >/dev/null
    aws lambda wait function-updated-v2 --function-name "$fn"
    retry_iam aws lambda update-function-configuration --function-name "$fn" --handler "$handler" \
      --environment "file://$ENV_FILE" ${durable:+--durable-config "$durable" --tracing-config Mode=Active}
  else
    retry_iam aws lambda create-function --function-name "$fn" --runtime python3.13 --architectures arm64 \
      --handler "$handler" --role "$ROLE_ARN" --zip-file "fileb://$BUILD/function.zip" \
      --timeout 120 --memory-size 1024 --environment "file://$ENV_FILE" \
      ${durable:+--durable-config "$durable" --tracing-config Mode=Active}
    aws lambda wait function-active-v2 --function-name "$fn"
  fi
  aws lambda wait function-updated-v2 --function-name "$fn"
}

say "Function $PLAIN_FN (ordinary Lambda)"
deploy_function "$PLAIN_FN" handler_plain.handler
# The demo shows what a retry does, so turn off Lambda's own async retries for this one.
aws lambda put-function-event-invoke-config --function-name "$PLAIN_FN" --maximum-retry-attempts 0 >/dev/null

say "Function $DURABLE_FN (durable)"
# ExecutionTimeout: the whole execution, across all invocations (up to 1 year).
# --timeout above: one invocation (up to 15 minutes).
deploy_function "$DURABLE_FN" "$DURABLE_HANDLER" '{"ExecutionTimeout": 86400, "RetentionPeriodInDays": 14}'

say "Publish a version and point alias 'live' at it"
VERSION="$(aws lambda publish-version --function-name "$DURABLE_FN" --query Version --output text)"
if aws lambda get-alias --function-name "$DURABLE_FN" --name live >/dev/null 2>&1; then
  aws lambda update-alias --function-name "$DURABLE_FN" --name live --function-version "$VERSION" >/dev/null
else
  aws lambda create-alias --function-name "$DURABLE_FN" --name live --function-version "$VERSION" >/dev/null
fi
echo "$DURABLE_FN:live -> version $VERSION. In-flight executions stay on the version they started on."
