#!/usr/bin/env bash
# Start the HTTP API locally with serverless-offline (stage: local).
# Compatibility wrapper; environment loading is shared with direct invocation.
set -euo pipefail

# shellcheck source=scripts/_common.sh
source "$(dirname "$0")/_common.sh"

cd "$ROOT"
exec poetry run python scripts/run_local.py "$@"
