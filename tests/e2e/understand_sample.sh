#!/bin/sh
# Real-data understanding of the sample chapter (docs/sample-chapter-source.md) through the API,
# over HTTPS through Caddy -> Prefect `chapter-understand` -> worker. Needs the running stack
# (make up-cloud), the Phase 0 test users and a ready chapter (make ingest-sample).
#
#   sh tests/e2e/understand_sample.sh                 TRIAL: the first TRIAL_PANELS panels, then stop
#   CONFIRM=yes sh tests/e2e/understand_sample.sh     the FULL chapter (the API only accepts it after
#                                                     a finished, current trial); never started
#                                                     without CONFIRM=yes
#
# Prints counts and timings only: never panel text, scene text, passwords or cookies. Passwords and
# cookies stay in a private temp dir. TLS is verified against Caddy's local root CA (copied out of
# the caddy container), never skipped. The chapter is UNDERSTAND_CHAPTER_ID, else the one in the
# newest data/reports/ingest-sample-*/report.json. The offline report goes to data/reports/.
set -eu

B="https://${KAGEBOX_HOST:-localhost}:${KAGEBOX_PORT:-8443}"
S=deploy/secrets
TRIAL_PANELS=${TRIAL_PANELS:-10}
CONFIRM=${CONFIRM:-no}
TIMEOUT_S=${UNDERSTAND_TIMEOUT_S:-5400}
POLL_S=${UNDERSTAND_POLL_S:-15}
COMPOSE="docker compose -f deploy/compose.yaml --env-file .env"
CA_PATH=/data/caddy/pki/authorities/local/root.crt
MAX_RELOGINS=3
case "$TRIAL_PANELS" in ''|*[!0-9]*) echo "FAIL  TRIAL_PANELS must be a number" >&2; exit 1 ;; esac

D=$(mktemp -d)
chmod 700 "$D"
SAMPLER=
cleanup() { [ -z "$SAMPLER" ] || kill "$SAMPLER" 2>/dev/null || true; rm -rf "$D"; }
trap cleanup EXIT
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
stage_field() { # field -> the understand stage row's field ('' when there is no row)
  python3 -I - "$D/out" "$1" <<'PY'
import json, sys
rows = [r for r in json.load(open(sys.argv[1])) if r["stage"] == "understand"]
v = rows[0].get(sys.argv[2]) if rows else None
print("" if v is None else v)
PY
}

if [ -n "${UNDERSTAND_CHAPTER_ID:-}" ]; then
  C=$UNDERSTAND_CHAPTER_ID
else
  last=$(ls -d data/reports/ingest-sample-*/report.json 2>/dev/null | sort | tail -1)
  [ -n "$last" ] || die "no UNDERSTAND_CHAPTER_ID and no ingest report to take it from"
  C=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["chapter_id"])' "$last")
fi
case "$C" in chap_*[!0-9a-z_]*|chap_) die "unexpected chapter id" ;; chap_*) ;; *) die "unexpected chapter id" ;; esac

login member member@kagebox.test
expect "chapter" 200 "$(req member GET "/chapters/$C")"
[ "$(json '["status"]')" = ready ] || die "chapter is not ready"

req member GET "/chapters/$C/stages" >/dev/null
before=$(stage_field finished_at)

if [ "$CONFIRM" = yes ]; then
  mode=full
  echo '{"confirm_full": true}' > "$D/start.json"
  want=done
else
  mode=trial
  python3 -I -c 'import json,sys; json.dump({"limit_panels": int(sys.argv[1])}, open(sys.argv[2], "w"))' \
    "$TRIAL_PANELS" "$D/start.json"
  want=pending  # a trial finishes as 'pending' with counts.trial, never 'done'
fi

# RSS of the worker while the run is in progress (container memory usage, sampled every 10 s)
WID=$($COMPOSE ps -q worker-cpu)
[ -n "$WID" ] || die "worker-cpu is not running"
( while :; do
    docker stats --no-stream --format '{{.MemUsage}}' "$WID" | awk '{print $1}' >> "$D/rss" || true
    sleep 10
  done ) &
SAMPLER=$!

start=$(date +%s)
expect "start $mode" 202 "$(req member POST "/chapters/$C/understand" \
  -H 'content-type: application/json' --data-binary @"$D/start.json")"

relogins=0
while :; do
  sleep "$POLL_S"
  elapsed=$(( $(date +%s) - start ))
  [ "$elapsed" -lt "$TIMEOUT_S" ] || die "understanding did not finish within ${TIMEOUT_S}s"
  code=$(req member GET "/chapters/$C/stages")
  if [ "$code" = 401 ]; then
    relogins=$((relogins + 1))
    [ "$relogins" -le "$MAX_RELOGINS" ] || die "session kept expiring ($MAX_RELOGINS re-logins)"
    login member member@kagebox.test
    continue
  fi
  [ "$code" = 200 ] || die "stages: HTTP $code"
  st=$(stage_field status)
  fin=$(stage_field finished_at)
  echo "      ${elapsed}s: ${st:-none}"
  [ "$st" != failed ] || die "understand failed: $(stage_field error)"
  if [ "$st" = "$want" ] && [ -n "$fin" ] && [ "$fin" != "$before" ]; then break; fi
done
wall=$(( $(date +%s) - start ))
kill "$SAMPLER" 2>/dev/null || true; SAMPLER=
echo "ok    $mode finished in ${wall}s"

python3 -I - "$D/out" <<'PY'
import json, sys
rows = [r for r in json.load(open(sys.argv[1])) if r["stage"] == "understand"]
c = rows[0]["counts"]
keys = ("panels", "scenes", "cached", "failed", "flagged", "seconds_per_panel", "project_total_s")
print("      stage counts: " + ", ".join(f"{k}={c.get(k)}" for k in keys))
PY
peak=$(sort -h "$D/rss" 2>/dev/null | tail -1 || true)
echo "      worker peak memory sampled: ${peak:-n/a}"

# What the API serves for the first panels: counts only (scene text is never printed).
req member GET "/chapters/$C/pages?limit=1000" >/dev/null
python3 -I -c 'import json,sys; [print(p["id"]) for p in json.load(open(sys.argv[1]))["items"]]' "$D/out" > "$D/pages"
: > "$D/summary"
n=0
while read -r pid && [ "$n" -lt "$TRIAL_PANELS" ]; do
  code=$(req member GET "/pages/$pid/panels")
  [ "$code" = 200 ] || die "panels: HTTP $code"
  python3 -I - "$D/out" "$D/summary" <<'PY'
import json, sys
with open(sys.argv[2], "a") as f:
    for p in json.load(open(sys.argv[1])):
        s = p["scene"]
        f.write(json.dumps(None if s is None else {"unknown": s["unknown_fields"], "figures": len(s["figures"])}) + "\n")
PY
  n=$(wc -l < "$D/summary" | tr -d ' ')
done < "$D/pages"
python3 -I - "$D/summary" "$TRIAL_PANELS" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])][: int(sys.argv[2])]
have = [r for r in rows if r is not None]
print(f"ok    API serves {len(have)} scenes for the first {len(rows)} panels; figures per panel: {[r['figures'] for r in have]}")
PY

stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="data/reports/understand-sample-$mode-$stamp"
mkdir -p "$out"
ROOT=$PWD
$COMPOSE --profile bakeoff run --rm --no-deps -T \
  -v "$ROOT/pipeline":/app/pipeline:ro -v "$ROOT/orchestration":/app/orchestration:ro \
  -v "$ROOT/$out":/out understand-bakeoff \
  python -m orchestration.understand_report "$C" --out /out --masked
echo "ok    report in $out"
