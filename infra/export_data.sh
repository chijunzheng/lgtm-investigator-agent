#!/usr/bin/env bash
# Export the LGTM data volume to a tarball for GCP deployment.
#
# Usage:
#   ./infra/export_data.sh                    # -> infra/lgtm-data.tar.gz
#   ./infra/export_data.sh /path/to/output    # -> /path/to/output/lgtm-data.tar.gz
#
# Prerequisites:
#   - Docker running
#   - LGTM containers stopped (docker compose -f infra/docker-compose.yml stop)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="${1:-${SCRIPT_DIR}}"
OUTPUT_FILE="${OUTPUT_DIR}/lgtm-data.tar.gz"
VOLUME_NAME="infra_lgtm-data"

# Check if volume exists
if ! docker volume inspect "${VOLUME_NAME}" > /dev/null 2>&1; then
    echo "[error] Volume '${VOLUME_NAME}' not found."
    echo "[error] Run setup.sh and seed_failures.py first."
    exit 1
fi

echo "[export] Exporting volume '${VOLUME_NAME}' to ${OUTPUT_FILE}..."
docker run --rm \
    -v "${VOLUME_NAME}":/data \
    -v "${OUTPUT_DIR}":/backup \
    alpine tar czf /backup/lgtm-data.tar.gz -C / data

SIZE=$(du -h "${OUTPUT_FILE}" | cut -f1)
echo "[export] Done. ${OUTPUT_FILE} (${SIZE})"
echo "[export] Upload to GCP: gcloud compute scp ${OUTPUT_FILE} <vm-name>:~/"
