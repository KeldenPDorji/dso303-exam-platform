#!/usr/bin/env bash
# Post-deploy smoke test through the real edge path: CloudFront -> WAF -> API Gateway -> Lambda -> RDS Proxy -> RDS
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SITE_URL="$("$ROOT/scripts/tf-outputs.sh" site_url)"

for i in $(seq 1 30); do   # a brand-new CloudFront distribution can take a few minutes
  if BODY="$(curl -fsS "$SITE_URL/api/health")"; then
    echo "API healthy: $BODY"
    curl -fsS "$SITE_URL/" | grep -q "<title>" && echo "Frontend OK: $SITE_URL"
    exit 0
  fi
  echo "waiting for $SITE_URL ($i/30)"; sleep 10
done
echo "Smoke test FAILED"; exit 1
