#!/usr/bin/env bash
# Shared helpers for run-local.sh / deploy.sh. Source this file, do not execute it.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

load_aws_credentials() {
  local aws_env_file="$ROOT/.env.aws"
  if [[ ! -f "$aws_env_file" ]]; then
    echo "ERROR: $aws_env_file not found. Run 'yarn aws:login' first." >&2
    exit 1
  fi
  if [[ ! -f "$ROOT/.env" ]]; then
    echo "ERROR: $ROOT/.env not found. Copy .env.example to .env and fill it in." >&2
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090
  source <(tr -d '\r' < "$aws_env_file")
  set +a
}

activate_poetry_venv() {
  local venv_path
  if ! venv_path="$(cd "$ROOT" && poetry env info --path 2>/dev/null)" || [[ -z "$venv_path" ]]; then
    echo "ERROR: Poetry virtualenv not found. Run 'yarn install:all' first." >&2
    exit 1
  fi
  # Windows venv uses Scripts/, Linux/macOS uses bin/
  export VIRTUAL_ENV="$venv_path"
  export PATH="$venv_path/Scripts:$venv_path/bin:$PATH"
}
