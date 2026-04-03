#!/usr/bin/env bash
# Import LGTM data tarball into a Docker volume on GCP VM.
#
# Usage (on the GCP VM):
#   ./import_data.sh ~/lgtm-data.tar.gz
#
# After import, start the production stack:
#   docker compose -f docker-compose.prod.yml up -d

set -euo pipefail

TARBALL="${1:?Usage: $0 <path-to-lgtm-data.tar.gz>}"
VOLUME_NAME="investigate-cli_lgtm-data"

if [ ! -f "${TARBALL}" ]; then
    echo "[error] File not found: ${TARBALL}"
    exit 1
fi

echo "[import] Creating Docker volume '${VOLUME_NAME}'..."
docker volume create "${VOLUME_NAME}"

echo "[import] Importing data from ${TARBALL}..."
docker run --rm \
    -v "${VOLUME_NAME}":/data \
    -v "$(dirname "$(realpath "${TARBALL}")")":/backup \
    alpine tar xzf "/backup/$(basename "${TARBALL}")" -C /

echo "[import] Done. Volume '${VOLUME_NAME}' populated."
echo "[import] Start LGTM: docker compose -f docker-compose.prod.yml up -d"
echo "[import] Verify:"
echo "  curl http://localhost:3100/ready"
echo "  curl http://localhost:9090/-/ready"
echo "  curl http://localhost:3200/ready"
