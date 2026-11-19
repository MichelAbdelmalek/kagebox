#!/bin/sh
# Real-data ingest of the sample chapter (docs/sample-chapter-source.md) through the API, over
# HTTPS through Caddy, then the offline report into data/reports/. Needs the running stack
# (make up-cloud) and the Phase 0 test users (see phase0_smoke.sh). Imports are scoped per project:
# the source is hard-linked (never copied, never symlinked: imports refuse symlinks) into
# data/imports/<project_id>/ once the project is known. Passwords and cookies stay in a private temp dir
# and are never printed. TLS is verified against Caddy's local root CA (copied out of the caddy
# container), never skipped. Set INGEST_CHAPTER_ID=chap_... to re-ingest an existing chapter
# (the flow replaces its results) instead of creating a new project, rights record and chapter.
set -eu

B="https://${KAGEBOX_HOST:-localhost}:${KAGEBOX_PORT:-8443}"
S=deploy/secrets
SRC_NAME=001bj.pdf
SRC=data/fixtures/blackjack-v1/work/$SRC_NAME
FIRST=3
LAST=58
SNAPSHOT=data/fixtures/blackjack-v1/terms/densho810-free-2026-11-19.html
SNAPSHOT_SHA=2bb1dc9f1a06de8ca66705af93b772f00633cd85c644bd7835025008d43b6da1
TIMEOUT_S=${INGEST_TIMEOUT_S:-2400}
COMPOSE="docker compose -f deploy/compose.yaml --env-file .env"
CA_PATH=/data/caddy/pki/authorities/local/root.crt
MAX_RELOGINS=3

D=$(mktemp -d)
chmod 700 "$D"
trap 'rm -rf "$D"' EXIT
$COMPOSE cp "caddy:$CA_PATH" "$D/ca.crt" >/dev/null
CURL="curl -s --cacert $D/ca.crt"

die() { echo "FAIL  $*" >&2; exit 1; }
csrf() { awk '/__Host-kb_csrf/{print $7}' "$D/$1.jar"; }
login() { # user email
  python3 -I - "$S/test_$1.password.txt" "$2" "$D/body" <<'PY'
import json, sys
pw = open(sys.argv[1], encoding="utf-8").read().strip()
json.dump({"email": sys.argv[2], "password": pw}, open(sys.argv[3], "w"))
PY
  code=$($CURL -c "$D/$1.jar" -H "Origin: $B" -H 'content-type: application/json' \
    -o /dev/null -w '%{http_code}' --data-binary @"$D/body" "$B/api/auth/login")
  rm -f "$D/body"
  [ "$code" = 200 ] || die "login $1: HTTP $code"
}
req() { # user method path [curl args...]  -> prints status, body in $D/out
  u=$1; m=$2; p=$3; shift 3
  $CURL -b "$D/$u.jar" -H "Origin: $B" -H "x-csrf-token: $(csrf "$u")" -X "$m" \
    -o "$D/out" -w '%{http_code}' "$@" "$B/api$p"
}
json() { python3 -I -c "import json,sys; print(json.load(open(sys.argv[1]))$1)" "$D/out"; }
expect() { # name expected actual
  [ "$2" = "$3" ] || { echo "FAIL  $1: expected $2, got $3" >&2; cat "$D/out" >&2; exit 1; }
  echo "ok    $1"
}

[ -f "$SRC" ] || die "$SRC missing (see docs/sample-chapter-source.md)"
stage() { # project id -> hard link of the source in that project's import directory
  case "$1" in proj_*[!0-9a-z_]*|proj_) die "unexpected project id" ;; proj_*) ;; *) die "unexpected project id" ;; esac
  mkdir -p "data/imports/$1"
  [ -f "data/imports/$1/$SRC_NAME" ] || ln "$SRC" "data/imports/$1/$SRC_NAME"
  echo "ok    source hard-linked into data/imports/$1/"
}
got=$(shasum -a 256 "$SNAPSHOT" 2>/dev/null || sha256sum "$SNAPSHOT")
[ "${got%% *}" = "$SNAPSHOT_SHA" ] || die "terms snapshot sha256 does not match the docs"

login member member@kagebox.test
login admin admin@kagebox.test

stamp=$(date -u +%Y%m%dT%H%M%SZ)
if [ -n "${INGEST_CHAPTER_ID:-}" ]; then
  C=$INGEST_CHAPTER_ID
  expect "existing chapter" 200 "$(req member GET "/chapters/$C")"
  P=$(json '["project_id"]')
else
  python3 -I -c 'import json,sys; json.dump({"title": sys.argv[1], "format": "manga_rtl"}, open(sys.argv[2], "w"))' \
    "Give My Regards to Black Jack vol. 1 (ingest sample $stamp)" "$D/project.json"
  expect "create project" 201 "$(req member POST /projects -H 'content-type: application/json' \
    --data-binary @"$D/project.json")"
  P=$(json '["id"]')

  # The rights endpoint takes PDF or images, not HTML: wrap the snapshot (attached unchanged).
  python3 -I tests/e2e/evidence_pdf.py "$SNAPSHOT" "$D/evidence.pdf" \
    "https://densho810.com/free/" "2026-11-19"
  expect "submit rights (member)" 201 "$(req member POST "/projects/$P/rights" \
    -F basis=licensed \
    --form-string 'licensor=有限会社佐藤漫画製作所' \
    --form-string "justification=free secondary-use terms (densho810.com/free/, rev. 2017-08-31), adaptation incl. anime permitted; evidence: PDF wrapping the terms snapshot of 2026-11-19 (sha256 $SNAPSHOT_SHA)" \
    -F allows_cloud_processing=false \
    -F "evidence=@$D/evidence.pdf;type=application/pdf")"
  expect "rights pending" pending "$(json '["status"]')"
  expect "admin approves rights" 200 "$(req admin POST "/projects/$P/rights/approve")"
  expect "rights approved" approved "$(json '["status"]')"

  expect "create chapter" 201 "$(req member POST "/projects/$P/chapters" \
    -H 'content-type: application/json' -d '{"number":1,"language":"ja"}')"
  C=$(json '["id"]')
fi
stage "$P"
start=$(date +%s)
python3 -I -c 'import json,sys; json.dump({"name": sys.argv[1], "page_start": int(sys.argv[2]), "page_end": int(sys.argv[3])}, open(sys.argv[4], "w"))' \
  "$SRC_NAME" "$FIRST" "$LAST" "$D/import.json"
expect "import source" 202 "$(req member POST "/chapters/$C/import" \
  -H 'content-type: application/json' --data-binary @"$D/import.json")"
echo "      chapter $C"

status=pending
relogins=0
while :; do
  sleep 15
  elapsed=$(( $(date +%s) - start ))
  [ "$elapsed" -lt "$TIMEOUT_S" ] || die "ingest did not finish within ${TIMEOUT_S}s"
  code=$(req member GET "/chapters/$C")
  if [ "$code" = 401 ]; then  # session expired on a very long run: log in again (capped)
    relogins=$((relogins + 1))
    [ "$relogins" -le "$MAX_RELOGINS" ] || die "session kept expiring ($MAX_RELOGINS re-logins)"
    login member member@kagebox.test
    continue
  fi
  [ "$code" = 200 ] || die "chapter status: HTTP $code"
  status=$(json '["status"]')
  echo "      ${elapsed}s: $status"
  case "$status" in ready|failed) break ;; esac
done
wall=$(( $(date +%s) - start ))
[ "$status" = ready ] || die "chapter failed: $(json '["error"]')"
echo "ok    chapter ready in ${wall}s"

code=$(req member GET "/chapters/$C/pages?limit=1000")
expect "list pages" 200 "$code"
expect "page count" $((LAST - FIRST + 1)) "$(json '["items"].__len__()')"

out="data/reports/ingest-sample-$stamp"
mkdir -p "$out"
$COMPOSE run --rm --no-deps -T -v "$PWD/$out":/reports worker-cpu \
  python -m orchestration.ingest_report "$C" --out /reports --wall-time-s "$wall"
echo "ok    report in $out"
