#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
env_file="${PITCHLOOP_ENV_FILE:-${repo_root}/infra/pomerium/.env}"
if [[ "${env_file}" != /* ]]; then
  env_file="${repo_root}/${env_file}"
fi

if [[ ! -f "${env_file}" ]]; then
  echo "ERROR environment file not found; set PITCHLOOP_ENV_FILE or create infra/pomerium/.env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a

: "${POMERIUM_DENIED_URL:?set POMERIUM_DENIED_URL}"
: "${POMERIUM_ALLOWED_URL:?set POMERIUM_ALLOWED_URL}"
: "${POMERIUM_SERVICE_ACCOUNT_TOKEN:?set POMERIUM_SERVICE_ACCOUNT_TOKEN}"

probe_tmp="$(mktemp -d "${TMPDIR:-/tmp}/pitchloop-policy-probe.XXXXXX")"
trap 'rm -rf "${probe_tmp}"' EXIT

auth_value="${POMERIUM_SERVICE_ACCOUNT_TOKEN}"
if [[ "${auth_value}" != Bearer\ * && "${auth_value}" != Pomerium\ * ]]; then
  if [[ "${auth_value}" == Pomerium-* ]]; then
    auth_value="Bearer ${auth_value}"
  else
    auth_value="Bearer Pomerium-${auth_value}"
  fi
fi

probe() {
  local label="$1"
  local candidate="$2"
  local url="$3"
  local expected_status="$4"
  local headers_file="${probe_tmp}/${label}.headers"
  local body_file="${probe_tmp}/${label}.body"
  local status

  status="$(curl \
    --silent \
    --show-error \
    --connect-timeout 5 \
    --max-time 15 \
    --max-redirs 0 \
    --request POST \
    --header 'Accept: application/json' \
    --header 'Content-Type: application/json' \
    --header "Authorization: ${auth_value}" \
    --data "{\"action\":\"place_sales_call\",\"candidate_id\":\"${candidate}\"}" \
    --dump-header "${headers_file}" \
    --output "${body_file}" \
    --write-out '%{http_code}' \
    "${url}")"

  local request_id
  request_id="$(awk 'BEGIN{IGNORECASE=1} /^x-request-id:/ {gsub("\\r", "", $2); print $2; exit}' "${headers_file}")"
  request_id="${request_id:-unavailable}"

  if [[ "${label}" == "DENIED" ]]; then
    printf 'DENIED candidate=%s status=%s request_id=%s\n' "${candidate}" "${status}" "${request_id}"
  else
    local reached
    reached="$(python3 -c 'import json,sys; print(str(bool(json.load(open(sys.argv[1])).get("reached_upstream"))).lower())' "${body_file}" 2>/dev/null || echo false)"
    printf 'ALLOWED candidate=%s status=%s upstream_reached=%s request_id=%s\n' "${candidate}" "${status}" "${reached}" "${request_id}"
    [[ "${reached}" == "true" ]] || return 1
  fi

  [[ "${status}" == "${expected_status}" ]]
}

probe DENIED alex_rivera "${POMERIUM_DENIED_URL}" 403
probe ALLOWED maya_chen "${POMERIUM_ALLOWED_URL}" 200
