#!/usr/bin/env bash
# Builds the Lambda dependency layer (pg8000, pure Python) into backend/build/layer/python.
# Terraform zips backend/src and this folder during `terraform apply`.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"

rm -rf "$ROOT/backend/build"
mkdir -p "$ROOT/backend/build/layer/python"
find "$ROOT/backend/src" -name "__pycache__" -prune -exec rm -rf {} +

# Target the Lambda runtime (Python 3.13 on arm64) regardless of the machine we build on.
"$PY" -m pip install --quiet --no-compile --disable-pip-version-check \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.13 --only-binary=:all: \
  --target "$ROOT/backend/build/layer/python" -r "$ROOT/backend/requirements.txt"

# RDS certificate authorities, so Lambda can verify TLS when connecting to the database directly
# (no RDS Proxy). Lambda mounts the layer at /opt, so this becomes /opt/rds-global-bundle.pem.
curl -fsSL https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem -o "$ROOT/backend/build/layer/rds-global-bundle.pem"

echo "Layer built: $(du -sh "$ROOT/backend/build/layer" | cut -f1)"
