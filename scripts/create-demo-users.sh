#!/usr/bin/env bash
# Creates one admin, one tutor and one student in Cognito with a permanent password,
# then seeds a demo assignment owned by the tutor.
# Usage: ./scripts/create-demo-users.sh admin@x.com tutor@x.com student@x.com 'Passw0rd-Demo-2026'
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ $# -ne 4 ]; then
  echo "Usage: $0 <admin-email> <tutor-email> <student-email> <password (12+ chars, upper, lower, digit)>"; exit 1
fi
POOL="$("$ROOT/scripts/tf-outputs.sh" user_pool_id)"
PASSWORD="$4"

create() {  # email group
  aws cognito-idp admin-create-user --user-pool-id "$POOL" --username "$1" --message-action SUPPRESS \
    --user-attributes Name=email,Value="$1" Name=email_verified,Value=true >/dev/null 2>&1 \
    || echo "  ($1 already exists)"
  aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username "$1" --password "$PASSWORD" --permanent
  aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" --username "$1" --group-name "$2"
  echo "  $1 -> $2"
}

echo "Creating demo users in $POOL"
create "$1" Admins
create "$2" Tutors
create "$3" Students

"$ROOT/scripts/migrate.sh" "$2" >/dev/null && echo "Demo exam + assignment seeded (tutor: $2)"
