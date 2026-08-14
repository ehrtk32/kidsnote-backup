#!/bin/zsh
set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/Library/Frameworks/Python.framework/Versions/3.11/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="${0:A:h}"
ROOT_DIR="${SCRIPT_DIR:h}"

# export_static_site.py parses .env itself, but wrangler only reads the
# environment — so CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID have to be
# loaded here. Split on the first '=' only: some values (WordPress app
# passwords) contain spaces, so `source .env` would break on them.
ENV_FILE="${SCRIPT_DIR}/.env"
if [[ -f "${ENV_FILE}" ]]; then
  while IFS='=' read -r key value; do
    key="${key## }"; key="${key%% }"
    [[ -z "${key}" || "${key}" == \#* ]] && continue
    value="${value%$'\r'}"
    value="${value#\"}"; value="${value%\"}"
    value="${value#\'}"; value="${value%\'}"
    export "${key}=${value}"
  done < "${ENV_FILE}"
fi
LOG_DIR="${ROOT_DIR}/logs"
LOCK_DIR="${LOG_DIR}/seoi-kidsnote-static.lock"
PROJECT_NAME="${CLOUDFLARE_PAGES_PROJECT:-seoi-kidsnote}"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="${LOG_DIR}/seoi-kidsnote-static-${TIMESTAMP}.log"
LATEST_LOG="${LOG_DIR}/seoi-kidsnote-static.latest"

mkdir -p "${LOG_DIR}"

if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  echo "Another static export is already running. Lock: ${LOCK_DIR}" | tee -a "${LOG_FILE}"
  exit 0
fi
trap 'rmdir "${LOCK_DIR}" 2>/dev/null || true' EXIT

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Start Seoi Kidsnote static export"
  cd "${SCRIPT_DIR}"
  python3 export_static_site.py

  if [[ "${STATIC_EXPORT_DEPLOY:-1}" == "1" ]]; then
    if [[ -z "${CLOUDFLARE_API_TOKEN:-}" ]]; then
      echo "CLOUDFLARE_API_TOKEN is not set. Add it to ${ENV_FILE} (Pages -> Edit token)."
      exit 1
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Deploy to Cloudflare Pages: ${PROJECT_NAME}"
    npx --yes wrangler pages deploy dist --project-name "${PROJECT_NAME}" \
      --branch main --commit-dirty=true
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Deploy skipped because STATIC_EXPORT_DEPLOY=${STATIC_EXPORT_DEPLOY:-}"
  fi

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done"
} 2>&1 | tee -a "${LOG_FILE}"

printf '%s\n' "${LOG_FILE}" > "${LATEST_LOG}"
