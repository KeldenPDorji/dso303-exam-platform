#!/usr/bin/env bash
# Invokes the migrate Lambda (inside the VPC) to apply the database schema and seed demo data.
# Usage: ./scripts/migrate.sh [tutor-email-for-demo-assignment]
# Retries because on a first deploy RDS Proxy can need a few minutes before its target is available.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FN="$("$ROOT/scripts/tf-outputs.sh" lambda_functions migrate)"
OUT="$(mktemp)"
PAYLOAD='{"seed": true}'
if [ -n "${1:-}" ]; then PAYLOAD="{\"seed\": true, \"tutorEmail\": \"$1\"}"; fi

for attempt in $(seq 1 10); do
  ERR="$(aws lambda invoke --function-name "$FN" --cli-binary-format raw-in-base64-out \
          --payload "$PAYLOAD" --query 'FunctionError' --output text "$OUT")"
  cat "$OUT"; echo
  if [ "$ERR" = "None" ] || [ -z "$ERR" ]; then echo "Migration OK"; exit 0; fi
  echo "Migration attempt $attempt failed (database not reachable yet?), retrying in 30 s"
  sleep 30
done
echo "Migration FAILED"; exit 1
