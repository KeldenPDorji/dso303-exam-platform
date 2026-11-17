#!/usr/bin/env bash
# Prints one Terraform output: ./scripts/tf-outputs.sh site_url
# For map outputs pass a key: ./scripts/tf-outputs.sh lambda_functions migrate
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ $# -eq 1 ]; then
  terraform -chdir="$ROOT/infra" output -raw "$1"
else
  terraform -chdir="$ROOT/infra" output -json "$1" | python3 -c "import json,sys; print(json.load(sys.stdin)[sys.argv[1]])" "$2"
fi
