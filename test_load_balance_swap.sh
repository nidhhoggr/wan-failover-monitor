#!/usr/bin/env bash
#
# test_load_balance_swap.sh
#
# Tests the actual failover write call: fetches the current Internet Load
# Balance config, swaps primaryWans/backupWan, and PUTs it back -- this
# WILL move live traffic to whichever WAN you specify as primary. Requires
# explicit typed confirmation before it does anything.
#
# Usage:
#   ./test_load_balance_swap.sh failover   # swap to WAN_BACKUP_PORT_ID as primary
#   ./test_load_balance_swap.sh failback   # swap back to WAN_PRIMARY_PORT_ID as primary
#   ./test_load_balance_swap.sh show       # just GET and print current config, no changes
#
# Env file defaults to ./.env. Override HTTP_METHOD if modifyInternetLoadBalance_1
# turns out not to be PUT (e.g. HTTP_METHOD=PATCH ./test_load_balance_swap.sh failover).

set -euo pipefail

ACTION="${1:-}"
ENV_FILE="${2:-.env}"
HTTP_METHOD="${HTTP_METHOD:-PUT}"

if [[ "$ACTION" != "failover" && "$ACTION" != "failback" && "$ACTION" != "show" ]]; then
    echo "Usage: $0 <failover|failback|show> [env-file]" >&2
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: env file not found at $ENV_FILE" >&2
    exit 1
fi

for bin in curl jq; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "Error: '$bin' is required but not installed." >&2
        exit 1
    fi
done

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${OMADA_BASE_URL:?OMADA_BASE_URL not set}"
: "${OMADA_CLIENT_ID:?OMADA_CLIENT_ID not set}"
: "${OMADA_CLIENT_SECRET:?OMADA_CLIENT_SECRET not set}"
: "${OMADA_OMADAC_ID:?OMADA_OMADAC_ID not set}"
: "${OMADA_SITE_ID:?OMADA_SITE_ID not set}"
: "${WAN_PRIMARY_PORT_ID:?WAN_PRIMARY_PORT_ID not set}"
: "${WAN_BACKUP_PORT_ID:?WAN_BACKUP_PORT_ID not set}"

CURL_TLS_OPTS=()
if [[ "${OMADA_VERIFY_TLS:-true}" == "false" ]]; then
    CURL_TLS_OPTS+=(-k)
fi

BASE_URL="${OMADA_BASE_URL%/}"
LB_PATH="/openapi/v1/${OMADA_OMADAC_ID}/sites/${OMADA_SITE_ID}/internet/load-balance"

# ---- auth --------------------------------------------------------------
echo "Requesting access token..." >&2
TOKEN_RESPONSE=$(curl -sS "${CURL_TLS_OPTS[@]}" -X POST \
    "${BASE_URL}/openapi/authorize/token?grant_type=client_credentials" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg o "$OMADA_OMADAC_ID" --arg i "$OMADA_CLIENT_ID" --arg s "$OMADA_CLIENT_SECRET" \
        '{omadacId: $o, client_id: $i, client_secret: $s}')")

if [[ "$(echo "$TOKEN_RESPONSE" | jq -r '.errorCode // "null"')" != "0" ]]; then
    echo "Auth failed:" >&2; echo "$TOKEN_RESPONSE" | jq '.' >&2; exit 1
fi
ACCESS_TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.result.accessToken')

# ---- fetch current state -------------------------------------------------
echo "Fetching current Internet Load Balance config..." >&2
CURRENT=$(curl -sS "${CURL_TLS_OPTS[@]}" -X GET "${BASE_URL}${LB_PATH}" \
    -H "Authorization: AccessToken=${ACCESS_TOKEN}" -H 'Content-Type: application/json')

if [[ "$(echo "$CURRENT" | jq -r '.errorCode // "null"')" != "0" ]]; then
    echo "GET failed:" >&2; echo "$CURRENT" | jq '.' >&2; exit 1
fi

CURRENT_RESULT=$(echo "$CURRENT" | jq '.result')
echo ""
echo "Current config:"
echo "$CURRENT_RESULT" | jq '.'
echo ""

if [[ "$ACTION" == "show" ]]; then
    exit 0
fi

# ---- build the swapped body ----------------------------------------------
if [[ "$ACTION" == "failover" ]]; then
    NEW_PRIMARY="$WAN_BACKUP_PORT_ID"
    NEW_BACKUP="$WAN_PRIMARY_PORT_ID"
else
    NEW_PRIMARY="$WAN_PRIMARY_PORT_ID"
    NEW_BACKUP="$WAN_BACKUP_PORT_ID"
fi

NEW_BODY=$(echo "$CURRENT_RESULT" | jq \
    --arg primary "$NEW_PRIMARY" --arg backup "$NEW_BACKUP" \
    '.primaryWans = [$primary] | .backupWan = $backup')

echo "This will send:"
echo "$NEW_BODY" | jq '.'
echo ""
echo "!! This is a LIVE write. Confirming will really move traffic to portId: $NEW_PRIMARY !!"
read -r -p "Type 'yes' to proceed with ${HTTP_METHOD} to ${LB_PATH}: " CONFIRM
if [[ "$CONFIRM" != "yes" ]]; then
    echo "Aborted, no changes made." >&2
    exit 1
fi

# ---- fire the write -------------------------------------------------------
RESPONSE=$(curl -sS "${CURL_TLS_OPTS[@]}" -X "$HTTP_METHOD" "${BASE_URL}${LB_PATH}" \
    -H "Authorization: AccessToken=${ACCESS_TOKEN}" -H 'Content-Type: application/json' \
    -d "$NEW_BODY")

echo ""
echo "Response:"
echo "$RESPONSE" | jq '.' 2>/dev/null || echo "$RESPONSE"

if [[ "$(echo "$RESPONSE" | jq -r '.errorCode // "null"' 2>/dev/null)" == "0" ]]; then
    echo ""
    echo "Success. Verifying by re-fetching config in 3s..." >&2
    sleep 3
    VERIFY=$(curl -sS "${CURL_TLS_OPTS[@]}" -X GET "${BASE_URL}${LB_PATH}" \
        -H "Authorization: AccessToken=${ACCESS_TOKEN}" -H 'Content-Type: application/json')
    echo "$VERIFY" | jq '.result'
fi
