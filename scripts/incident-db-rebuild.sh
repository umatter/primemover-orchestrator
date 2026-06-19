#!/bin/bash
#
# Incident recovery: rebuild the wiped MySQL database from scratch.
#
# Context: the `primemover` DB was wiped by the Akila MySQL-ransom bot
# (exposed 0.0.0.0:3307 + MYSQL_ALLOW_EMPTY_PASSWORD). Compose has been
# hardened (localhost binding, empty-password removed) and credentials rotated
# (values staged in ~/.pm-rotation-secrets). This script:
#   1. tears down the API stack
#   2. WIPES the mysql + redis data dirs (purges any attacker root-level
#      persistence: rogue users, UDFs, modified system tables)
#   3. brings the stack back up -> MySQL re-initialises with the new password
#   4. regenerates APP_KEY, runs migrate:fresh --seed
#   5. rotates the seeded admin@primemover.example password to the new app password
#   6. verifies login over the now-localhost-only API
#
# Run from the host (you are the owner of the docker socket):
#   bash scripts/incident-db-rebuild.sh
#
set -uo pipefail

SECRETS=/home/primemover/.pm-rotation-secrets
API_DIR=/home/primemover/primemover_api
DATA=$API_DIR/storage/app/data
PHP=primemover-php8.1
MYSQL=primemover-mysql

[ -f "$SECRETS" ] || { echo "FATAL: $SECRETS not found"; exit 1; }
# shellcheck disable=SC1090
source "$SECRETS"   # provides DBPASS, REDISPASS, APPPASS

cd "$API_DIR" || exit 1

echo "==> [1/6] Stopping API stack"
docker compose down

echo "==> [2/6] Wiping mysql + redis data dirs (attacker had root; full purge)"
docker run --rm \
  -v "$DATA/mysql:/m" -v "$DATA/redis:/r" \
  alpine sh -c 'rm -rf /m/* /m/.[!.]* /m/..?* /r/* /r/.[!.]* /r/..?* 2>/dev/null; true'
echo "    mysql dir entries left: $(ls -A "$DATA/mysql" 2>/dev/null | wc -l)"

echo "==> [3/6] Starting API stack (MySQL fresh-init with rotated password)"
docker compose up -d

echo "    waiting for MySQL to accept the new credentials..."
ok=0
for i in $(seq 1 90); do
  if docker exec "$MYSQL" mysqladmin ping -uroot -p"$DBPASS" --silent >/dev/null 2>&1; then ok=1; echo "    MySQL ready (${i}x2s)"; break; fi
  sleep 2
done
[ "$ok" = 1 ] || { echo "FATAL: MySQL did not come up with the new password"; exit 1; }

echo "    confirming ransom DB is gone:"
docker exec "$MYSQL" mysql -uroot -p"$DBPASS" -N -e 'SHOW DATABASES;' 2>/dev/null | sed 's/^/      /'

echo "==> [4/6] Regenerating APP_KEY + migrate:fresh --seed"
docker exec "$PHP" php artisan key:generate --force
docker exec "$PHP" php artisan migrate:fresh --seed --force

echo "==> [5/6] Rotating seeded admin password to the new app password"
docker exec -e NEWPW="$APPPASS" "$PHP" php artisan tinker --execute='
  $u = App\Models\User::where("email","admin@primemover.example")->first();
  if (!$u) { echo "ERROR: admin user not found\n"; exit; }
  $u->password = bcrypt(getenv("NEWPW"));
  $u->save();
  echo "admin@primemover.example password updated\n";
'

echo "==> [6/6] Verifying login over the API (localhost-only)"
CODE=$(curl -s -o /tmp/pm-login.json -w '%{http_code}' -X POST http://localhost:8090/api/v1/login \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"admin@primemover.example\",\"password\":\"$APPPASS\"}")
echo "    login HTTP status: $CODE"
if [ "$CODE" = "200" ]; then
  echo "    ✓ Auth works with rotated credentials"
else
  echo "    ✗ Auth FAILED — inspect /tmp/pm-login.json"; head -c 400 /tmp/pm-login.json; echo
fi

echo ""
echo "Done. If login is 200, run:  orchestrator status -v  then the e2e suite."
