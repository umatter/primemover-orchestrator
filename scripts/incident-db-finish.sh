#!/bin/bash
#
# Finish the half-completed DB rebuild.
#
# The first rebuild left primemover-php8.1 Exited(0) (it caught a shutdown
# signal during the down/up churn; restart=on-failure does not restart a
# clean exit), so migrate/seed never ran and the `primemover` DB has 0 tables.
# This brings PHP back up and runs the migration/seed/admin-password steps.
#
#   bash scripts/incident-db-finish.sh
#
set -uo pipefail
SECRETS=/home/primemover/.pm-rotation-secrets
API_DIR=/home/primemover/primemover_api
PHP=primemover-php8.1
# shellcheck source=/dev/null  # runtime secrets file, not in the repo
source "$SECRETS"
cd "$API_DIR" || exit 1

echo "==> Ensuring the API stack (incl. PHP) is up"
docker compose up -d
sleep 5
state=$(docker inspect -f '{{.State.Status}}' "$PHP" 2>/dev/null)
echo "    php container state: $state"
if [ "$state" != "running" ]; then
  echo "    PHP not running — restarting it explicitly"
  docker compose restart php
  sleep 5
  state=$(docker inspect -f '{{.State.Status}}' "$PHP" 2>/dev/null)
  echo "    php container state: $state"
fi
[ "$state" = "running" ] || { echo "FATAL: PHP won't stay up. Logs:"; docker logs "$PHP" 2>&1 | tail -20; exit 1; }

echo "==> Clearing stale cached config (bootstrap/cache/config.php pins old DB password 'secret' and overrides .env)"
docker exec "$PHP" php artisan config:clear
docker exec "$PHP" php artisan cache:clear 2>/dev/null || true

echo "==> key:generate + migrate:fresh --seed"
docker exec "$PHP" php artisan key:generate --force
docker exec "$PHP" php artisan migrate:fresh --seed --force

echo "==> Installing Passport OAuth clients (migrate:fresh wiped them)"
docker exec "$PHP" php artisan passport:install --force

echo "==> Rotating seeded admin password"
docker exec -e NEWPW="$APPPASS" "$PHP" php artisan tinker --execute='
  $u = App\Models\User::where("email","admin@primemover.example")->first();
  if(!$u){echo "ERROR: admin not found\n";exit;}
  $u->password = bcrypt(getenv("NEWPW")); $u->save();
  echo "admin password rotated\n";'

echo "==> Table count now:"
docker exec primemover-mysql mysql -uroot -p"$DBPASS" -N \
  -e 'SELECT COUNT(*) FROM information_schema.tables WHERE table_schema="primemover";' 2>/dev/null | sed 's/^/    tables: /'

echo "==> Login check"
CODE=$(curl -s -o /tmp/pm-login.json -w '%{http_code}' -X POST http://localhost:8090/api/v1/login \
  -H 'Content-Type: application/json' -d "{\"email\":\"admin@primemover.example\",\"password\":\"$APPPASS\"}")
echo "    login HTTP status: $CODE"
[ "$CODE" = 200 ] && echo "    ✓ API recovered" || { echo "    ✗ still failing:"; head -c 400 /tmp/pm-login.json; echo; }
