#!/bin/bash
#
# Safely set the runner's Multilogin credentials.
# Prompts (no echo), VERIFIES against api.multilogin.com, and only writes the
# config if Multilogin accepts them. Avoids sed special-char mangling and never
# puts the password in shell history or the process list.
#
#   bash scripts/set-multilogin-pw.sh
#
CONF=/home/primemover/primemover_runner/primemover-runner/config/application.properties

read -rp  'Multilogin email: ' MLUSER
read -rsp 'Multilogin password: ' MLPW; echo

MD5=$(printf '%s' "$MLPW" | md5sum | cut -d' ' -f1)
CODE=$(curl -s -o /tmp/mlx-check.json -w '%{http_code}' \
  -X POST https://api.multilogin.com/user/signin \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$MLUSER\",\"password\":\"$MD5\"}")

if [ "$CODE" = 200 ]; then
  MLUSER="$MLUSER" MLPW="$MLPW" python3 - "$CONF" <<'PY'
import os, sys, pathlib
conf = pathlib.Path(sys.argv[1])
u, p = os.environ['MLUSER'], os.environ['MLPW']
out = []
for line in conf.read_text().splitlines():
    if line.startswith('multilogin.apiUser='):       out.append('multilogin.apiUser=' + u)
    elif line.startswith('multilogin.apiPassword='):  out.append('multilogin.apiPassword=' + p)
    else:                                             out.append(line)
conf.write_text('\n'.join(out) + '\n')
PY
  chmod 600 "$CONF"
  echo "✓ Multilogin accepted the credentials — written to config (chmod 600)"
else
  echo "✗ Multilogin REJECTED these credentials (HTTP $CODE) — config left unchanged"
  python3 -c "import json;print('   message:',json.load(open('/tmp/mlx-check.json')).get('status',{}).get('message',''))" 2>/dev/null
fi
unset MLPW
rm -f /tmp/mlx-check.json
