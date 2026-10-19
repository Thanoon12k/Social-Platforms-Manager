#!/usr/bin/env bash
# Upload the app to PythonAnywhere (/home/$PA_USER/spm), point the web app's WSGI file at it, reload.
#   PA_USER=myname PA_TOKEN=xxxx ./deploy.sh      (token: pythonanywhere.com > Account > API token)
# Create the web app once first (Web tab > Add > Manual config, Python 3.10+), and run
#   pip install --user -r /home/$PA_USER/spm/requirements.txt   in a PythonAnywhere console.
set -euo pipefail
U="${PA_USER:?}"; T="${PA_TOKEN:?}"; D="${PA_DOMAIN:-$U.pythonanywhere.com}"
H="${PA_HOST:-www.pythonanywhere.com}"; A="https://$H/api/v0/user/$U"
cd "$(dirname "$0")"

pa() {  # retry: the API throttles bursts (HTTP 429)
  for i in 1 2 3 4 5 6; do
    curl -sf -H "Authorization: Token $T" "$@" >/dev/null && return 0
    sleep $((i * 10))
  done
  echo "FAILED: $*" >&2; return 1
}

for f in $(git ls-files | grep -v -E '^(deploy.sh|\.gitignore|\.github/.*|tests/.*)$'); do
  pa -F "content=@$f" "$A/files/path/home/$U/spm/$f" && echo "up $f"
done
printf 'import sys\nsys.path.insert(0, "/home/%s/spm")\nfrom wsgi import application  # noqa\n' "$U" > /tmp/spm_wsgi.py
pa -F "content=@/tmp/spm_wsgi.py" "$A/files/path/var/www/${D//./_}_wsgi.py"
pa -X POST "$A/webapps/$D/reload/" && echo "reloaded https://$D"
