#!/usr/bin/env bash
# AWS SSO login + export temporary credentials to <repo>/.env.aws (git-ignored).
# .env.aws is read by run-local.sh / deploy.sh / scripts/invoke_local.py.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/.env"
AWS_ENV_FILE="$ROOT/.env.aws"

if [[ -z "${AWS_SSO_PROFILE:-}" && -f "$ENV_FILE" ]]; then
  AWS_SSO_PROFILE="$(grep -E '^AWS_SSO_PROFILE=' "$ENV_FILE" | tail -n 1 | cut -d '=' -f 2- | tr -d '\r"')"
fi

if [[ -z "${AWS_SSO_PROFILE:-}" ]]; then
  echo "ERROR: AWS_SSO_PROFILE is not set. Add it to .env (see .env.example)." >&2
  exit 1
fi

echo "==> AWS SSO login (profile: $AWS_SSO_PROFILE)..."
aws sso login --profile "$AWS_SSO_PROFILE"

echo "==> Exporting temporary credentials..."
aws configure export-credentials --profile "$AWS_SSO_PROFILE" --format env-no-export > "$AWS_ENV_FILE"

echo "==> Credentials saved to $AWS_ENV_FILE"
