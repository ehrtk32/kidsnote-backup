#!/bin/zsh
set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/Library/Frameworks/Python.framework/Versions/3.11/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="${0:A:h}"
ROOT_DIR="${SCRIPT_DIR:h}"
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
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Deploy to Cloudflare Pages: ${PROJECT_NAME}"
    npx --yes wrangler pages deploy dist --project-name "${PROJECT_NAME}"
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Deploy skipped because STATIC_EXPORT_DEPLOY=${STATIC_EXPORT_DEPLOY:-}"
  fi

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done"
} 2>&1 | tee -a "${LOG_FILE}"

printf '%s\n' "${LOG_FILE}" > "${LATEST_LOG}"
