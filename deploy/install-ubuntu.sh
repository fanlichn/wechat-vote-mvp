#!/usr/bin/env bash
set -euo pipefail

# Run: sudo bash deploy/install-ubuntu.sh
if [[ "$(id -u)" != 0 ]]; then
  echo 'Run this installer as root.' >&2
  exit 1
fi
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ "$source_dir" != /root/clf/wechat/wechat-vote-mvp ]]; then
  echo 'Place this project at /root/clf/wechat/wechat-vote-mvp first.' >&2
  exit 1
fi
if ! /usr/bin/python3 -c 'import sys,sqlite3; assert sys.version_info >= (3,10)' ; then
  echo 'Install the system Python first: apt-get update && apt-get install -y python3' >&2
  exit 1
fi
if [[ ! -e /etc/wechat-vote.env ]]; then
  # Create without overwriting an existing administrator password.
  /usr/bin/python3 - <<'PY'
import os
import secrets
fd = os.open('/etc/wechat-vote.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write('VOTE_ADMIN_PASSWORD=' + secrets.token_urlsafe(24) + '\n')
PY
fi
chmod 600 /etc/wechat-vote.env
install -d -m 700 /var/lib/wechat-vote
install -m 644 "$source_dir/deploy/wechat-vote.service" /etc/systemd/system/wechat-vote.service
systemctl daemon-reload
systemctl enable wechat-vote.service
systemctl restart wechat-vote.service
sleep 1
systemctl is-active --quiet wechat-vote.service
/usr/bin/python3 - <<'PY'
import json
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:18080/api/status', timeout=5) as response:
    result = json.load(response)
print('Backend ready; phase:', result['phase'])
PY
echo 'Backend installed. Admin password: cat /etc/wechat-vote.env'
echo 'Listening on 0.0.0.0:18080; Nginx is not required.'
echo 'Allow TCP 18080 in your cloud security group and active firewall.'
echo 'Public URL: http://YOUR_PUBLIC_IP:18080/'
