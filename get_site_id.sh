#!/usr/bin/env bash
#
# get_site_id.sh
#
# Fetches an Omada Open API access token using the client-credentials in
# .env, then lists sites visible to that app -- so you can find the real
# siteId to put in OMADA_SITE_ID. Mirrors the same auth flow as
# omada_client.py, just as a standalone bash script for quick lookups
# without spinning up the container.
#
# Requires: curl, jq
#
# Usage:
#   ./get_site_id.sh            # reads ./.env
#   ./get_site_id.sh /path/.env # reads a specific env file

set -euo pipefail

ENV_FILE="${1:-.env}"

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

# Load .env without polluting the whole shell -- only the OMADA_* vars we need.
# set -a exports everything sourced, set +a turns that back off immediately after.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${OMADA_BASE_URL:?OMADA_BASE_URL not set in $ENV_FILE}"
: "${OMADA_CLIENT_ID:?OMADA_CLIENT_ID not set in $ENV_FILE}"
: "${OMADA_CLIENT_SECRET:?OMADA_CLIENT_SECRET not set in $ENV_FILE}"
: "${OMADA_OMADAC_ID:?OMADA_OMADAC_ID not set in $ENV_FILE}"

# OMADA_VERIFY_TLS=false in .env -> pass curl -k (insecure). Defaults to
# verifying, same default posture as omada_client.py.
CURL_TLS_OPTS=()
if [[ "${OMADA_VERIFY_TLS:-true}" == "false" ]]; then
    CURL_TLS_OPTS+=(-k)
fi

BASE_URL="${OMADA_BASE_URL%/}"

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

echo "Got access token. Fetching site list..." >&2

SITES_RESPONSE=$(curl -sS "${CURL_TLS_OPTS[@]}" \
    -X GET \
    "${BASE_URL}/openapi/v1/${OMADA_OMADAC_ID}/sites?pageSize=100&page=1" \
    -H "Authorization: AccessToken=${ACCESS_TOKEN}" \
    -H 'Content-Type: application/json')

ERROR_CODE=$(echo "$SITES_RESPONSE" | jq -r '.errorCode // "null"')
if [[ "$ERROR_CODE" != "0" ]]; then
    echo "Error: sites request failed." >&2
    echo "$SITES_RESPONSE" | jq '.' >&2
    exit 1
fi

SITE_COUNT=$(echo "$SITES_RESPONSE" | jq -r '.result.data | length // 0')
if [[ "$SITE_COUNT" == "0" ]]; then
    echo "No sites returned. This app's Site Privilege may not include any sites --" >&2
    echo "check Global View > Settings > Platform Integration > Open API on the controller." >&2
    echo "$SITES_RESPONSE" | jq '.'
    exit 1
fi

echo ""
echo "Sites visible to this app:"
echo "$SITES_RESPONSE" | jq -r '.result.data[] | "  \(.name)\tsiteId=\(.siteId)"'
echo ""
echo "Copy the siteId for the site containing your ER605 into OMADA_SITE_ID in $ENV_FILE."
