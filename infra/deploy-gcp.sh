#!/usr/bin/env bash
# Deploy the complete Investigate app to a GCP VM.
#
# Deploys: LGTM (data backend) + FastAPI (agent) + Caddy (reverse proxy)
# Result:  A single URL the interviewer can open to test the full app.
#
# Prerequisites:
#   - gcloud CLI authenticated (gcloud auth login)
#   - A GCP project selected (gcloud config set project <PROJECT_ID>)
#   - lgtm-data.tar.gz exported (./infra/export_data.sh)
#   - OPENAI_API_KEY set in environment or .env
#
# Usage:
#   ./infra/deploy-gcp.sh
#   ./infra/deploy-gcp.sh --name my-vm --zone us-west1-b
#
# Why GCE instead of Cloud Run?
#   The LGTM stack needs 1.5GB of persistent data and multiple internal ports.
#   Cloud Run is designed for stateless HTTP services. A single GCE VM with
#   a Caddy reverse proxy gives the same single-URL experience with less complexity.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- Defaults ---
VM_NAME="investigate-app"
ZONE="us-central1-a"
MACHINE_TYPE="e2-standard-2"  # 2 vCPU, 8 GB RAM
DISK_SIZE="30GB"

# --- Parse flags ---
while [[ $# -gt 0 ]]; do
    case $1 in
        --name) VM_NAME="$2"; shift 2 ;;
        --zone) ZONE="$2"; shift 2 ;;
        --machine-type) MACHINE_TYPE="$2"; shift 2 ;;
        *) echo "Unknown flag: $1"; exit 1 ;;
    esac
done

# --- Verify prerequisites ---
if ! command -v gcloud &> /dev/null; then
    echo "[error] gcloud CLI not found. Install: https://cloud.google.com/sdk/docs/install"
    exit 1
fi

DATA_FILE="${SCRIPT_DIR}/lgtm-data.tar.gz"
if [ ! -f "${DATA_FILE}" ]; then
    echo "[error] ${DATA_FILE} not found."
    echo "[error] Run: ./infra/export_data.sh"
    exit 1
fi

# Load OPENAI_API_KEY from .env if not in environment
if [ -z "${OPENAI_API_KEY:-}" ]; then
    ENV_FILE="${PROJECT_ROOT}/.env"
    if [ -f "${ENV_FILE}" ]; then
        OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' "${ENV_FILE}" | cut -d= -f2-)
    fi
fi
if [ -z "${OPENAI_API_KEY:-}" ]; then
    echo "[error] OPENAI_API_KEY not set. Add it to .env or export it."
    exit 1
fi

PROJECT=$(gcloud config get-value project 2>/dev/null)
echo "=== Deploying Investigate App to GCP ==="
echo "  Project:  ${PROJECT}"
echo "  VM:       ${VM_NAME}"
echo "  Zone:     ${ZONE}"
echo "  Machine:  ${MACHINE_TYPE}"
echo ""

# --- Step 1: Create VM ---
echo "[1/6] Creating VM with Docker support..."
gcloud compute instances create "${VM_NAME}" \
    --zone="${ZONE}" \
    --machine-type="${MACHINE_TYPE}" \
    --boot-disk-size="${DISK_SIZE}" \
    --image-family=ubuntu-2204-lts \
    --image-project=ubuntu-os-cloud \
    --tags=investigate-app \
    --metadata=startup-script='#!/bin/bash
        apt-get update -qq
        apt-get install -y -qq docker.io docker-compose-v2 > /dev/null 2>&1
        systemctl enable docker
        systemctl start docker
        usermod -aG docker $(logname 2>/dev/null || echo ubuntu)
    ' \
    --quiet

echo "  Waiting for VM startup (installing Docker)..."
sleep 45

# --- Step 2: Upload project files ---
echo "[2/6] Uploading project files..."
# Create a staging tarball of just the files needed for deployment
STAGING=$(mktemp -d)
mkdir -p "${STAGING}/src" "${STAGING}/infra"
cp -r "${PROJECT_ROOT}/src/"* "${STAGING}/src/"
cp "${PROJECT_ROOT}/requirements.txt" "${STAGING}/"
cp "${SCRIPT_DIR}/docker-compose.deploy.yml" "${STAGING}/infra/"
cp "${SCRIPT_DIR}/Caddyfile" "${STAGING}/infra/"
cp "${SCRIPT_DIR}/import_data.sh" "${STAGING}/infra/"

tar czf "${STAGING}/app.tar.gz" -C "${STAGING}" src requirements.txt infra
gcloud compute scp "${STAGING}/app.tar.gz" "${VM_NAME}:~/" --zone="${ZONE}" --quiet
rm -rf "${STAGING}"

echo "  Uploading lgtm-data.tar.gz ($(du -h "${DATA_FILE}" | cut -f1))..."
gcloud compute scp "${DATA_FILE}" "${VM_NAME}:~/" --zone="${ZONE}" --quiet

# --- Step 3: Set up on VM ---
echo "[3/6] Setting up application on VM..."
gcloud compute ssh "${VM_NAME}" --zone="${ZONE}" --quiet --command="
    # Extract app files
    mkdir -p ~/investigate-cli
    tar xzf ~/app.tar.gz -C ~/investigate-cli

    # Import LGTM data
    chmod +x ~/investigate-cli/infra/import_data.sh
    ~/investigate-cli/infra/import_data.sh ~/lgtm-data.tar.gz
"

# --- Step 4: Create .env and start services ---
echo "[4/6] Starting services..."
gcloud compute ssh "${VM_NAME}" --zone="${ZONE}" --quiet --command="
    cd ~/investigate-cli

    # Write .env for docker compose
    cat > .env << EOF
OPENAI_API_KEY=${OPENAI_API_KEY}
AGENT_VERSION=v4
EOF

    # Start all services
    docker compose -f infra/docker-compose.deploy.yml up -d

    echo 'Waiting 60s for all services to start...'
    sleep 60

    echo 'Health checks:'
    docker exec lgtm wget -qO- http://localhost:3100/ready 2>/dev/null && echo '  Loki: OK' || echo '  Loki: starting...'
    docker exec lgtm wget -qO- http://localhost:9090/-/ready 2>/dev/null && echo '  Mimir: OK' || echo '  Mimir: starting...'
    docker exec lgtm wget -qO- http://localhost:3200/ready 2>/dev/null && echo '  Tempo: OK' || echo '  Tempo: starting...'
"

# --- Step 5: Open firewall ---
echo "[5/6] Creating firewall rule (port 80)..."
gcloud compute firewall-rules create allow-investigate-app \
    --allow=tcp:80 \
    --target-tags=investigate-app \
    --description="Allow HTTP access to Investigate app" \
    --quiet 2>/dev/null || echo "  (firewall rule already exists)"

# --- Step 6: Print URL ---
EXTERNAL_IP=$(gcloud compute instances describe "${VM_NAME}" \
    --zone="${ZONE}" \
    --format='get(networkInterfaces[0].accessConfigs[0].natIP)')

echo ""
echo "========================================"
echo "  Deployment complete"
echo "========================================"
echo ""
echo "  App URL:  http://${EXTERNAL_IP}"
echo ""
echo "  This URL provides:"
echo "    /grafana/   Grafana UI (browse telemetry, use Investigate plugin)"
echo "    /ws         Agent WebSocket (used by the plugin automatically)"
echo "    /api/demos  Demo scenarios API"
echo ""
echo "  Grafana login: admin / admin"
echo ""
echo "  To tear down:"
echo "    gcloud compute instances delete ${VM_NAME} --zone=${ZONE} --quiet"
echo "    gcloud compute firewall-rules delete allow-investigate-app --quiet"
