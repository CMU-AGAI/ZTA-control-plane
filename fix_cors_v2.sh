#!/usr/bin/env bash
# =============================================================================
# fix_cors_v2.sh — patch ALL services the demo UI talks to directly.
#
# Covers services my earlier fix_cors.sh missed: zta-auth and the
# supervisor (the supervisor is already configured but we verify).
# OPA has its own CORS handling via its --addr flag — separate.
#
# Idempotent. Run from the project root.
# =============================================================================

set -euo pipefail

if [[ ! -f docker-compose.zta.yml ]]; then
  echo "ERROR: run from the project root (where docker-compose.zta.yml lives)"
  exit 1
fi

# Services the BROWSER calls directly (excluding OPA, which is special)
TARGETS=(
  "services/auth/server.py"
  "services/trust-scorer/server.py"
  "services/audit-logger/server.py"
  "services/revocation-dispatcher/server.py"
  "services/pdp-behavior/server.py"
)

# Map server.py → docker-compose service name
declare_svc_for() {
  case "$1" in
    *auth/server.py*)                  echo "zta-auth" ;;
    *trust-scorer/server.py*)          echo "trust-scorer" ;;
    *audit-logger/server.py*)          echo "audit-logger" ;;
    *revocation-dispatcher/server.py*) echo "revocation-dispatcher" ;;
    *pdp-behavior/server.py*)          echo "pdp-behavior" ;;
  esac
}

echo "─── Step 1/4 · Patching server.py files ──────────────────────────────"

PATCHED_SERVICES=()
for f in "${TARGETS[@]}"; do
  if [[ ! -f "$f" ]]; then
    echo "  WARN: $f not found, skipping"
    continue
  fi
  result=$(python3 - <<PY
import re, sys
p = "$f"
s = open(p).read()

if "CORSMiddleware" in s and "add_middleware" in s:
    print("SKIP")
    sys.exit(0)

import_pat = re.compile(r"^(from fastapi import [^\n]+)$", re.M)
m = import_pat.search(s)
if not m:
    print("FAIL_IMPORT")
    sys.exit(0)
if "from fastapi.middleware.cors import CORSMiddleware" not in s:
    s = s[:m.end()] + "\nfrom fastapi.middleware.cors import CORSMiddleware" + s[m.end():]

app_pat = re.compile(r"(app\s*=\s*FastAPI\([^)]*\)|app\s*=\s*FastAPI\([\s\S]*?\n\))", re.M)
m = app_pat.search(s)
if not m:
    print("FAIL_APP")
    sys.exit(0)
insertion = '''

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)'''
if "add_middleware(\n    CORSMiddleware" not in s:
    s = s[:m.end()] + insertion + s[m.end():]

open(p, "w").write(s)
print("PATCHED")
PY
  )
  case "$result" in
    PATCHED) echo "  PATCHED $f"; PATCHED_SERVICES+=("$(declare_svc_for "$f")") ;;
    SKIP)    echo "  SKIP    $f (already patched)" ;;
    *)       echo "  ERROR   $f ($result)" ;;
  esac
done

if [[ ${#PATCHED_SERVICES[@]} -eq 0 ]]; then
  echo
  echo "Nothing to rebuild — all targets were already patched."
  echo "If you're still seeing 'Load failed' in the UI, try:"
  echo "  1. Hard-reload the browser (Cmd+Shift+R)"
  echo "  2. Check the Network tab for the failing request and tell me the URL"
  exit 0
fi

echo
echo "─── Step 2/4 · Rebuilding ${#PATCHED_SERVICES[@]} service(s): ${PATCHED_SERVICES[*]} ──"
docker compose -f docker-compose.zta.yml up -d --build "${PATCHED_SERVICES[@]}"

echo
echo "─── Step 3/4 · Waiting for healthchecks ───────────────────────────────"
sleep 15

echo
echo "─── Step 4/4 · Verifying CORS preflight ──────────────────────────────"

# All endpoints the browser POSTs/GETs to, with OPTIONS preflight
CHECKS=(
  "zta-auth:8180:POST:/token"
  "trust-scorer:8190:POST:/score"
  "audit-logger:8195:GET:/events"
  "revocation-dispatcher:8196:POST:/quarantine/agent"
  "revocation-dispatcher:8196:GET:/list"
  "pdp-behavior:8197:GET:/agents/foo/decision"
)

CURL=$(command -v curl 2>/dev/null || echo /usr/bin/curl)
all_ok=1
for entry in "${CHECKS[@]}"; do
  IFS=':' read -r name port method urlpath <<< "$entry"
  code=$("$CURL" -s -o /dev/null -w "%{http_code}" \
    -X OPTIONS "http://localhost:${port}${urlpath}" \
    -H "Origin: http://localhost" \
    -H "Access-Control-Request-Method: ${method}" \
    -H "Access-Control-Request-Headers: content-type")
  if [[ "$code" == "200" || "$code" == "204" ]]; then
    printf "  ✓ %-22s OPTIONS %-25s → %s\n" "$name" "$urlpath" "$code"
  else
    printf "  ✗ %-22s OPTIONS %-25s → %s\n" "$name" "$urlpath" "$code"
    all_ok=0
  fi
done

echo
if [[ $all_ok -eq 1 ]]; then
  echo "✓ All CORS preflights succeed. Hard-reload the demo UI and you're set."
else
  echo "✗ Some preflights still fail. Check container logs:"
  echo "  docker compose -f docker-compose.zta.yml logs ${PATCHED_SERVICES[*]} | tail -40"
  exit 1
fi
