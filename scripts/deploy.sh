#!/usr/bin/env bash
# Deploy to AWS. Usage: bash scripts/deploy.sh <stage>
set -euo pipefail

STAGE="${1:-}"
if [[ -z "$STAGE" || "$STAGE" == "local" ]]; then
  echo "ERROR: specify a deploy stage (e.g. dev). 'local' is for serverless-offline only." >&2
  exit 1
fi

# shellcheck source=scripts/_common.sh
source "$(dirname "$0")/_common.sh"

load_aws_credentials
activate_poetry_venv

cd "$ROOT"
echo "==> Deploying stage: $STAGE"
yarn sls deploy --stage "$STAGE"
