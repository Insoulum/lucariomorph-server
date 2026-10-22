#!/usr/bin/env bash
# ============================================================================
# Lucario Morph Sync — integration tests of the service (no Docker).
#
# Starts four service.py instances and a Mojang stub (mojang_stub.py):
#   A) main:       RATE_PER_MIN=1000                      — API, authorization, tokens
#   B) rate-limit: RATE_PER_MIN=5                         — 429 check
#   C) storage:    MAX_RECORDS=1                          — 507 check
#   D) ttl:        TTL_HOURS=0.003 (11 s), HOUSEKEEPING_SECONDS=2 — expiration check
# and runs every check. The summary is printed at the end: PASS/FAIL.
#
# Run:  bash tests/test_auth_flow.sh
# ============================================================================
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
WORK="$(mktemp -d)"
PASS=0; FAIL=0

U1="069a79f4-44e9-4726-a5be-fca90e38aaf5"   # any valid UUID for the tests
U2="11111111-2222-3333-4444-555555555555"
U3="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
NAME="TestPlayer"

# --- helpers -----------------------------------------------------------------
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'; }

jsonget() { python3 -c "import json,sys;d=json.load(sys.stdin);print(d$1)" 2>/dev/null; }

check() { # check <number> <description> <condition (0 = ok)>
  local n="$1" desc="$2" cond="$3"
  if [ "$cond" -eq 0 ]; then
    PASS=$((PASS+1)); printf 'ok   %2s  %s\n' "$n" "$desc"
  else
    FAIL=$((FAIL+1)); printf 'FAIL %2s  %s\n' "$n" "$desc"
  fi
}

code() { # HTTP status code
  curl -s -o /dev/null -w '%{http_code}' "$@" 2>/dev/null
}
body() {
  curl -s "$@" 2>/dev/null
}

# --- starting the services ---------------------------------------------------
STUB_PORT=$(free_port); A_PORT=$(free_port); B_PORT=$(free_port); C_PORT=$(free_port); D_PORT=$(free_port)

python3 "$HERE/mojang_stub.py" "$STUB_PORT" > "$WORK/stub.log" 2>&1 &
STUB_PID=$!

DATA_A="$WORK/dataA"; DATA_B="$WORK/dataB"; DATA_C="$WORK/dataC"; DATA_D="$WORK/dataD"
mkdir -p "$DATA_A" "$DATA_B" "$DATA_C" "$DATA_D"

PORT="$A_PORT" DATA_DIR="$DATA_A" RATE_PER_MIN=1000 \
  MOJANG_SESSION="http://127.0.0.1:$STUB_PORT" \
  python3 "$ROOT/service.py" > "$WORK/a.log" 2>&1 &
A_PID=$!
PORT="$B_PORT" DATA_DIR="$DATA_B" RATE_PER_MIN=5 \
  MOJANG_SESSION="http://127.0.0.1:$STUB_PORT" \
  python3 "$ROOT/service.py" > "$WORK/b.log" 2>&1 &
B_PID=$!
PORT="$C_PORT" DATA_DIR="$DATA_C" RATE_PER_MIN=1000 MAX_RECORDS=1 \
  MOJANG_SESSION="http://127.0.0.1:$STUB_PORT" \
  python3 "$ROOT/service.py" > "$WORK/c.log" 2>&1 &
C_PID=$!
PORT="$D_PORT" DATA_DIR="$DATA_D" RATE_PER_MIN=1000 TTL_HOURS=0.003 HOUSEKEEPING_SECONDS=2 \
  MOJANG_SESSION="http://127.0.0.1:$STUB_PORT" \
  python3 "$ROOT/service.py" > "$WORK/d.log" 2>&1 &
D_PID=$!

cleanup() { kill "$A_PID" "$B_PID" "$C_PID" "$D_PID" "$STUB_PID" 2>/dev/null; wait 2>/dev/null; }
trap cleanup EXIT

wait_up() { # wait_up <port>
  for _ in $(seq 1 50); do
    [ "$(code "http://127.0.0.1:$1/health")" = "200" ] && return 0
    sleep 0.1
  done
  return 1
}

wait_up "$A_PORT" || { echo "service A failed to start"; cat "$WORK/a.log"; exit 1; }
wait_up "$B_PORT" || { echo "service B failed to start"; cat "$WORK/b.log"; exit 1; }
wait_up "$C_PORT" || { echo "service C failed to start"; cat "$WORK/c.log"; exit 1; }
wait_up "$D_PORT" || { echo "service D failed to start"; cat "$WORK/d.log"; exit 1; }

A="http://127.0.0.1:$A_PORT"

echo "== Lucario Morph Sync: integration tests =="
echo "==   main service :$A_PORT  rate-limit service :$B_PORT  storage service :$C_PORT  ttl service :$D_PORT"
echo "==   Mojang stub :$STUB_PORT"
echo

# --- 1. basic API ------------------------------------------------------------
n=1
[ "$(code "$A/health")" = "200" ]; check $n "GET /health -> 200" $?
n=2
[ "$(body "$A/health" | jsonget "['ok']")" = "True" ]; check $n "GET /health -> ok:true" $?
n=3
[ "$(body "$A/v1/morphs?uuids=$U1")" = "{}" ]; check $n "reading an empty store -> {}" $?

n=4
r=$(code -X PUT "$A/v1/morphs/$U1" -H 'Content-Type: application/json' -d '{"variant": "lucariocaptain"}')
[ "$r" = "200" ]; check $n "PUT of a valid variant -> 200" $?
n=5
[ "$(body "$A/v1/morphs?uuids=$U1" | jsonget "['$U1']")" = "lucariocaptain" ]; check $n "the stored variant reads back" $?
n=6
body "$A/v1/morphs?uuids=$U2" | jsonget "['$U2']" > /dev/null 2>&1
[ "$(body "$A/v1/morphs?uuids=$U2")" = "{}" ]; check $n "a foreign/unknown UUID is absent from the response" $?

n=7
[ "$(code -X PUT "$A/v1/morphs/$U1" -H 'Content-Type: application/json' -d '{"variant": "lucarioholiday_shiny"}')" = "200" ]
check $n "PUT of another valid variant (shiny) -> 200" $?

n=8
[ "$(code -X PUT "$A/v1/morphs/$U1" -H 'Content-Type: application/json' -d '{"variant": "not_a_variant"}')" = "422" ]
check $n "PUT of an unknown variant -> 422" $?
n=9
[ "$(code -X PUT "$A/v1/morphs/not-a-uuid" -H 'Content-Type: application/json' -d '{"variant": "lucario"}')" = "400" ]
check $n "PUT with a malformed UUID -> 400" $?
n=10
[ "$(code -X PUT "$A/v1/morphs/$U1")" = "400" ]; check $n "PUT without a body -> 400" $?
n=11
[ "$(code -X PUT "$A/v1/morphs/$U1" -H 'Content-Type: application/json' -d '{broken')" = "400" ]
check $n "PUT with broken JSON -> 400" $?

# --- 2. clearing a morph and UUID canonicalization ---------------------------
n=12
[ "$(code -X DELETE "$A/v1/morphs/$U1")" = "200" ] && [ "$(body "$A/v1/morphs?uuids=$U1")" = "{}" ]
check $n "DELETE clears the morph (and reading confirms it)" $?
n=13
code -X PUT "$A/v1/morphs/$U1" -H 'Content-Type: application/json' -d '{"variant": "riolu"}' > /dev/null
[ "$(code -X PUT "$A/v1/morphs/$U1" -H 'Content-Type: application/json' -d '{"variant": ""}')" = "200" ] \
  && [ "$(body "$A/v1/morphs?uuids=$U1")" = "{}" ]
check $n 'PUT with an empty variant clears the morph' $?
n=14
URN="urn:uuid:$U1"
code -X PUT "$A/v1/morphs/$URN" -H 'Content-Type: application/json' -d '{"variant": "lucariomega"}' > /dev/null
[ "$(body "$A/v1/morphs?uuids=$U1" | jsonget "['$U1']")" = "lucariomega" ]
check $n "UUID canonicalization: the urn:uuid form lands in the same record" $?

# --- 3. record authorization via Mojang (through the stub) -------------------
auth() { # auth <uuid> <name> <uuid-no-dashes-or-empty> -> prints the token or nothing
  local uuid="$1" name="$2" join_profile="$3"
  local sid tok
  sid=$(body -X POST "$A/v1/auth/challenge" -H 'Content-Type: application/json' \
        -d "{\"uuid\": \"$uuid\", \"name\": \"$name\"}" | jsonget "['serverId']")
  [ -n "$sid" ] || return 1
  if [ -n "$join_profile" ]; then
    code -X POST "http://127.0.0.1:$STUB_PORT/session/minecraft/join" \
      -H 'Content-Type: application/json' \
      -d "{\"accessToken\": \"stub-token\", \"selectedProfile\": \"$join_profile\", \"serverId\": \"$sid\"}" > /dev/null
  fi
  body -X POST "$A/v1/auth/verify" -H 'Content-Type: application/json' \
    -d "{\"uuid\": \"$uuid\", \"name\": \"$name\", \"serverId\": \"$sid\"}"
}

n=15
sid=$(body -X POST "$A/v1/auth/challenge" -H 'Content-Type: application/json' \
      -d "{\"uuid\": \"$U3\", \"name\": \"$NAME\"}" | jsonget "['serverId']")
expr "$sid" : '^[0-9a-f]\{40\}$' > /dev/null 2>&1
check $n "challenge issues a serverId of 40 hex characters" $?

n=16
[ "$(code -X POST "$A/v1/auth/challenge" -H 'Content-Type: application/json' -d "{\"uuid\": \"$U3\", \"name\": \"bad name!\"}")" = "400" ]
check $n "challenge with a malformed name -> 400" $?

n=17
[ "$(code -X POST "$A/v1/auth/verify" -H 'Content-Type: application/json' \
    -d "{\"uuid\": \"$U3\", \"name\": \"$NAME\", \"serverId\": \"$(printf 'a%.0s' $(seq 40))\"}")" = "400" ] \
  || [ "$(body -X POST "$A/v1/auth/verify" -H 'Content-Type: application/json' \
    -d "{\"uuid\": \"$U3\", \"name\": \"$NAME\", \"serverId\": \"$(printf 'a%.0s' $(seq 40))\"}")" = "403" ]
check $n "verify without a prior join -> 403 (Mojang did not confirm)" $?

n=18
tok1=$(auth "$U3" "$NAME" "$(echo "$U3" | tr -d '-')")
tok1=$(printf '%s' "$tok1" | jsonget "['token']")
[ -n "$tok1" ] && [ ${#tok1} -ge 32 ]; check $n "challenge + join + verify -> a write token is issued" $?

n=19
[ "$(code -X PUT "$A/v1/morphs/$U3" -H 'Content-Type: application/json' -d '{"variant": "lucario"}')" = "401" ]
check $n "PUT to a protected UUID without a token -> 401" $?
n=20
[ "$(code -X PUT "$A/v1/morphs/$U3" -H 'Content-Type: application/json' -H "X-Lucario-Token: $tok1" -d '{"variant": "lucario"}')" = "200" ]
check $n "PUT to a protected UUID with the token -> 200" $?
n=21
tok2=$(auth "$U3" "$NAME" "$(echo "$U3" | tr -d '-')" | jsonget "['token']")
[ -n "$tok2" ] && [ "$tok2" = "$tok1" ]; check $n "re-authorization returns the SAME token" $?

# --- 4. rate limit (service B: RATE_PER_MIN=5) --------------------------------
n=22
rl="429"
for _ in 1 2 3 4 5; do code "http://127.0.0.1:$B_PORT/health" > /dev/null; done
[ "$(code "http://127.0.0.1:$B_PORT/health")" = "429" ]; check $n "exceeding RATE_PER_MIN -> 429" $?

# --- 5. storage overflow (service C: MAX_RECORDS=1) ---------------------------
n=23
code -X PUT "http://127.0.0.1:$C_PORT/v1/morphs/$U1" -H 'Content-Type: application/json' -d '{"variant": "lucario"}' > /dev/null
[ "$(code -X PUT "http://127.0.0.1:$C_PORT/v1/morphs/$U2" -H 'Content-Type: application/json' -d '{"variant": "lucario"}')" = "507" ]
check $n "MAX_RECORDS overflow -> 507 storage full" $?

# --- 6. record TTL (service D: TTL_HOURS=0.003 ≈ 11 s, HOUSEKEEPING_SECONDS=2) -
n=24
code -X PUT "http://127.0.0.1:$D_PORT/v1/morphs/$U1" -H 'Content-Type: application/json' -d '{"variant": "lucariomega"}' > /dev/null
[ "$(body "http://127.0.0.1:$D_PORT/v1/morphs?uuids=$U1" | jsonget "['$U1']")" = "lucariomega" ]
check $n "a fresh record (TTL service) reads right after the PUT" $?

n=25
sleep 14   # 11 s TTL + up to 2 s housekeeping + margin
[ "$(body "http://127.0.0.1:$D_PORT/v1/morphs?uuids=$U1")" = "{}" ]
check $n "a record older than TTL is removed by the housekeeping" $?

# --- summary ------------------------------------------------------------------
echo
echo "==================================================="
echo "TOTAL: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
