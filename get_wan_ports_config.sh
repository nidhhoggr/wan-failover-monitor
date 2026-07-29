#!/usr/bin/env bash
#
# get_wan_ports_config.sh
#
# Calls the Omada Open API "Wired Network" WAN port config endpoint(s)
# using the client-credentials in .env. Same auth flow as get_site_id.sh.
#
# WHY THIS SCRIPT TAKES A PATH ARGUMENT instead of hardcoding one:
# Knife4j's doc.html shows the exact path template for each operation
# (e.g. GET /openapi/v1/{omadacId}/sites/{siteId}/gateways/{gatewayMac}/wan-port-configs
# -- the real template may differ from that guess). Copy the path template
# shown directly above the "Try it out" button for getWanPortsConfig, with
# its {placeholders} intact, and pass it as the first argument. This script
# fills in {omadacId} and {siteId} from .env automatically, and {gatewayMac}
# from OMADA_GATEWAY_MAC if that placeholder appears.
#
# Usage:
#   ./get_wan_ports_config.sh '/openapi/v1/{omadacId}/sites/{siteId}/gateways/{gatewayMac}/wan-port-configs'
#   ./get_wan_ports_config.sh '/openapi/v1/{omadacId}/sites/{siteId}/wired-networks/wan/ports' /path/to/.env
#
# If you're not sure of the exact placeholder name Knife4j uses for the
# gateway MAC (gatewayMac vs mac vs deviceMac), just check the "Parameters"
# table on the same doc page -- whatever it's called there, substitute it
# in the path you pass here, or edit the PLACEHOLDER substitution block
# below to match.

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 '<endpoint-path-with-placeholders>' [env-file]" >&2
    echo "Example: $0 '/openapi/v1/{omadacId}/sites/{siteId}/gateways/{gatewayMac}/wan-port-configs'" >&2
    exit 1
fi

ENDPOINT_TEMPLATE="$1"
ENV_FILE="${2:-.env}"

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

: "${OMADA_BASE_URL:?OMADA_BASE_URL not set in $ENV_FILE}"
: "${OMADA_CLIENT_ID:?OMADA_CLIENT_ID not set in $ENV_FILE}"
: "${OMADA_CLIENT_SECRET:?OMADA_CLIENT_SECRET not set in $ENV_FILE}"
: "${OMADA_OMADAC_ID:?OMADA_OMADAC_ID not set in $ENV_FILE}"
: "${OMADA_SITE_ID:?OMADA_SITE_ID not set in $ENV_FILE}"

CURL_TLS_OPTS=()
if [[ "${OMADA_VERIFY_TLS:-true}" == "false" ]]; then
    CURL_TLS_OPTS+=(-k)
fi

BASE_URL="${OMADA_BASE_URL%/}"

# ---- fill in path placeholders --------------------------------------
# Handles the common placeholder spellings Knife4j tends to use. If your
# doc page uses something different (e.g. {deviceMac}), add a line here.
PATH_FILLED="$ENDPOINT_TEMPLATE"
PATH_FILLED="${PATH_FILLED//\{omadacId\}/$OMADA_OMADAC_ID}"
PATH_FILLED="${PATH_FILLED//\{siteId\}/$OMADA_SITE_ID}"
if [[ -n "${OMADA_GATEWAY_MAC:-}" ]]; then
    PATH_FILLED="${PATH_FILLED//\{gatewayMac\}/$OMADA_GATEWAY_MAC}"
    PATH_FILLED="${PATH_FILLED//\{mac\}/$OMADA_GATEWAY_MAC}"
    PATH_FILLED="${PATH_FILLED//\{deviceMac\}/$OMADA_GATEWAY_MAC}"
fi

if [[ "$PATH_FILLED" == *"{"* ]]; then
    echo "Warning: path still has unfilled placeholders after substitution:" >&2
    echo "  $PATH_FILLED" >&2
    echo "Check the Parameters table on the Knife4j doc page for the exact" >&2
    echo "placeholder name and edit this script's substitution block, or set" >&2
    echo "OMADA_GATEWAY_MAC in $ENV_FILE if that's the missing piece." >&2
fi

# ---- auth ------------------------------------------------------------
echo "Requesting access token from ${BASE_URL}..." >&2

TOKEN_RESPONSE=$(curl -sS "${CURL_TLS_OPTS[@]}" \
    -X POST \
    "${BASE_URL}/openapi/authorize/token?grant_type=client_credentials" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n \
        --arg omadacId "$OMADA_OMADAC_ID" \
        --arg clientId "$OMADA_CLIENT_ID" \
        --arg clientSecret "$OMADA_CLIENT_SECRET" \
        '{omadacId: $omadacId, client_id: $clientId, client_secret: $clientSecret}')")

ERROR_CODE=$(echo "$TOKEN_RESPONSE" | jq -r '.errorCode // "null"')
if [[ "$ERROR_CODE" != "0" ]]; then
    echo "Error: token request failed." >&2
    echo "$TOKEN_RESPONSE" | jq '.' >&2
    exit 1
fi

ACCESS_TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.result.accessToken')
if [[ -z "$ACCESS_TOKEN" || "$ACCESS_TOKEN" == "null" ]]; then
    echo "Error: no accessToken in response:" >&2
    echo "$TOKEN_RESPONSE" | jq '.' >&2
    exit 1
fi

# ---- call the actual endpoint ----------------------------------------
FULL_URL="${BASE_URL}${PATH_FILLED}"
echo "Calling GET ${FULL_URL}" >&2

RESPONSE=$(curl -sS "${CURL_TLS_OPTS[@]}" \
    -X GET \
    "$FULL_URL" \
    -H "Authorization: AccessToken=${ACCESS_TOKEN}" \
    -H 'Content-Type: application/json')

ERROR_CODE=$(echo "$RESPONSE" | jq -r '.errorCode // "null"')
if [[ "$ERROR_CODE" != "0" ]]; then
    echo "" >&2
    echo "Request failed (errorCode=$ERROR_CODE). Full response:" >&2
    echo "$RESPONSE" | jq '.'
    exit 1
fi

echo ""
echo "$RESPONSE" | jq '.result'
