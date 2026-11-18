#!/bin/sh
# Phase 0 smoke test against the running stack (make up-cloud), over HTTPS through Caddy.
# Needs admin@kagebox.test (admin) and member@kagebox.test (member), with their passwords in
# deploy/secrets/test_{admin,member}.password.txt (gitignored). Exits non-zero if any check fails.
set -eu

B="https://${KAGEBOX_HOST:-localhost}:${KAGEBOX_PORT:-8443}"
S=deploy/secrets
D=$(mktemp -d)
trap 'rm -rf "$D"' EXIT
fail=0

expect() { # name expected actual
  if [ "$2" = "$3" ]; then echo "ok    $1 ($3)"; else echo "FAIL  $1: expected $2, got $3"; fail=1; fi
}
csrf() { awk '/__Host-kb_csrf/{print $7}' "$D/$1.jar"; }
login() {
  printf '{"email":"%s","password":"%s"}' "$2" "$(cat "$S/test_$1.password.txt")" > "$D/body"
  curl -sk -c "$D/$1.jar" -H "Origin: $B" -H 'content-type: application/json' \
    -o /dev/null -w '%{http_code}' --data-binary @"$D/body" "$B/api/auth/login"
}
req() { # user method path [curl args...]  -> prints status, body in $D/out
  u=$1; m=$2; p=$3; shift 3
  curl -sk -b "$D/$u.jar" -H "Origin: $B" -H "x-csrf-token: $(csrf "$u")" -X "$m" \
    -o "$D/out" -w '%{http_code}' "$@" "$B/api$p"
}
json() { python3 -c "import json,sys; print(json.load(open('$D/out'))$1)"; }

expect "health" 200 "$(curl -sk -o /dev/null -w '%{http_code}' "$B/api/health")"
expect "unauthenticated /projects" 401 "$(curl -sk -o /dev/null -w '%{http_code}' "$B/api/projects")"
expect "web redirects to login" 307 "$(curl -sk -o /dev/null -w '%{http_code}' "$B/projects")"
csp=$(curl -sk -D - -o /dev/null "$B/login" | tr -d '\r' | grep -i '^content-security-policy:' || true)
case "$csp" in *"script-src 'self' 'nonce-"*) expect "web CSP with nonce" yes yes ;;
  *) expect "web CSP with nonce" yes no ;; esac
expect "login member" 200 "$(login member member@kagebox.test)"
expect "login admin" 200 "$(login admin admin@kagebox.test)"

expect "create project" 201 "$(req member POST /projects -H 'content-type: application/json' \
  -d '{"title":"Smoke test","format":"comic_ltr"}')"
P=$(json '["id"]')
expect "write without CSRF token" 403 "$(curl -sk -b "$D/member.jar" -H "Origin: $B" \
  -o /dev/null -w '%{http_code}' -H 'content-type: application/json' \
  -d '{"title":"x","format":"comic_ltr"}' "$B/api/projects")"
expect "cross-origin write" 403 "$(curl -sk -b "$D/member.jar" -H 'Origin: https://evil.example' \
  -H "x-csrf-token: $(csrf member)" -o /dev/null -w '%{http_code}' \
  -H 'content-type: application/json' -d '{"title":"x","format":"comic_ltr"}' "$B/api/projects")"

python3 - "$D/e.png" <<'PY'
import struct, sys, zlib
def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
raw = zlib.compress((b"\x00" + b"\xff" * 12) * 4)
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
open(sys.argv[1], "wb").write(png + chunk(b"IDAT", raw) + chunk(b"IEND", b""))
PY
printf '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>' > "$D/x.svg"

expect "disguised SVG rejected" 422 "$(req member POST "/projects/$P/rights" \
  -F basis=owned -F justification=x -F "evidence=@$D/x.svg;type=image/png")"
expect "submit rights" 201 "$(req member POST "/projects/$P/rights" \
  -F basis=public_domain -F 'justification=Smoke test fixture' -F "evidence=@$D/e.png")"
expect "rights status" pending "$(json '["status"]')"
expect "render before approval" 403 "$(req member POST "/projects/$P/render")"
expect "member cannot approve" 403 "$(req member POST "/projects/$P/rights/approve")"
expect "admin approves" 200 "$(req admin POST "/projects/$P/rights/approve")"
expect "render after approval" 202 "$(req member POST "/projects/$P/render")"

req member GET "/projects/$P" >/dev/null
U=$(json '["rights"]["evidence_url"]')
expect "evidence via signed link" 200 "$(curl -sk -b "$D/member.jar" -o /dev/null -w '%{http_code}' "$B$U")"
expect "signed link is user-bound" 403 "$(curl -sk -b "$D/admin.jar" -o /dev/null -w '%{http_code}' "$B$U")"

exit $fail
