#!/usr/bin/env bash
# Automated local environment setup for the Investigate CLI.
#
# What it does:
#   1. Starts the LGTM observability backend (Loki, Grafana, Tempo, Mimir)
#   2. Clones the OTel demo (microservices that generate telemetry)
#   3. Configures the demo's collector to export to LGTM
#   4. Starts the OTel demo
#   5. Waits for baseline telemetry to accumulate
#   6. Verifies data in each backend
#
# Prerequisites: Docker Desktop running, ports 3000-3200 + 4317-4318 free.
# Runtime: ~5 min setup + 10 min baseline wait.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEMO_DIR="${SCRIPT_DIR}/opentelemetry-demo"
BASELINE_WAIT=600  # 10 minutes for baseline telemetry

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[setup]${NC} $*"; }
warn()  { echo -e "${YELLOW}[setup]${NC} $*"; }
error() { echo -e "${RED}[setup]${NC} $*" >&2; }

# --- Step 1: Start LGTM backend ---
info "Starting LGTM backend..."
docker compose -f "${SCRIPT_DIR}/docker-compose.yml" up -d

info "Waiting for LGTM health check..."
# Grafana may be on 3001 if 3000 is occupied
GRAFANA_PORT=3000
if ! curl -sf http://localhost:3000/api/health > /dev/null 2>&1; then
    GRAFANA_PORT=3001
fi
for i in $(seq 1 30); do
    if curl -sf "http://localhost:${GRAFANA_PORT}/api/health" > /dev/null 2>&1; then
        info "Grafana is healthy on port ${GRAFANA_PORT}."
        break
    fi
    if [ "$i" -eq 30 ]; then
        error "LGTM failed to start after 30 attempts."
        exit 1
    fi
    sleep 2
done

# --- Step 2: Clone OTel demo ---
if [ -d "${DEMO_DIR}" ]; then
    info "OTel demo already cloned at ${DEMO_DIR}"
else
    info "Cloning OTel demo (depth=1)..."
    git clone --depth 1 https://github.com/open-telemetry/opentelemetry-demo.git "${DEMO_DIR}"
fi

# --- Step 3: Configure collector to export to LGTM ---
EXTRAS_FILE="${DEMO_DIR}/src/otel-collector/otelcol-config-extras.yml"
info "Installing collector extras config -> ${EXTRAS_FILE}"
cp "${SCRIPT_DIR}/otelcol-lgtm-export.yaml" "${EXTRAS_FILE}"

# On Linux (no Docker Desktop), replace host.docker.internal with bridge IP
if [[ "$(uname)" == "Linux" ]]; then
    BRIDGE_IP=$(docker network inspect bridge -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null || echo "172.17.0.1")
    warn "Linux detected. Using bridge IP ${BRIDGE_IP} instead of host.docker.internal"
    sed -i "s/host.docker.internal/${BRIDGE_IP}/g" "${EXTRAS_FILE}"
fi

# --- Step 3b: Create port override and compose override ---
cat > "${DEMO_DIR}/.env.investigate" << 'ENVEOF'
# Remap demo backends that conflict with LGTM on host
PROMETHEUS_PORT=19090
GRAFANA_PORT=13000
ENVEOF

cat > "${DEMO_DIR}/docker-compose.override.yml" << 'YMLEOF'
# Override: replace OpenSearch with stub (LGTM handles logs)
services:
  otel-collector:
    depends_on:
      jaeger:
        condition: service_started
  opensearch:
    image: alpine:3.19
    container_name: opensearch
    command: ["sh", "-c", "sleep infinity"]
    deploy:
      resources:
        limits:
          memory: 10M
    healthcheck:
      test: ["CMD", "true"]
      interval: 5s
      retries: 1
YMLEOF

# --- Step 4: Start OTel demo ---
info "Starting OTel demo services..."
cd "${DEMO_DIR}"
docker compose --env-file .env --env-file .env.investigate up -d --no-build 2>&1 | tail -5

info "Waiting 30s for services to initialize..."
sleep 30

# --- Step 5: Verify LGTM is receiving data ---
info "Verifying data flow to LGTM..."

verify_backend() {
    local name=$1
    local url=$2
    local check=$3
    local result
    result=$(curl -sf "${url}" 2>/dev/null || echo "FAIL")
    if echo "${result}" | grep -q "${check}"; then
        info "  ${name}: receiving data"
        return 0
    else
        warn "  ${name}: no data yet (may need more time)"
        return 1
    fi
}

BACKENDS_OK=0

# Loki: check for any log streams
verify_backend "Loki" \
    'http://localhost:3100/loki/api/v1/label/service_name/values' \
    "values" && BACKENDS_OK=$((BACKENDS_OK + 1))

# Prometheus: check for any metrics
verify_backend "Prometheus" \
    'http://localhost:9090/api/v1/label/__name__/values' \
    "values" && BACKENDS_OK=$((BACKENDS_OK + 1))

# Tempo: check for any traces
verify_backend "Tempo" \
    'http://localhost:3200/api/search?limit=1' \
    "traces" && BACKENDS_OK=$((BACKENDS_OK + 1))

if [ "${BACKENDS_OK}" -lt 3 ]; then
    warn ""
    warn "Not all backends have data yet. This is normal if the demo just started."
    warn "Wait ${BASELINE_WAIT}s for baseline telemetry, then re-verify:"
    warn "  curl http://localhost:3100/loki/api/v1/label/service_name/values"
    warn "  curl http://localhost:9090/api/v1/label/__name__/values"
    warn "  curl 'http://localhost:3200/api/search?limit=1'"
    warn ""
    warn "Or open Grafana at http://localhost:3000 (admin/admin) to explore."
fi

# --- Step 6: Print service list ---
info ""
info "Discovering service names from Tempo..."
curl -sf 'http://localhost:3200/api/search/tag/service.name/values' 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); [print(f'  - {v}') for v in sorted(d.get('tagValues',[]))]" \
    2>/dev/null || warn "  (no services found yet -- check again after baseline wait)"

info ""
info "Setup complete."
info "  Grafana:    http://localhost:${GRAFANA_PORT} (admin/admin)"
info "  Loki:       http://localhost:3100"
info "  Prometheus: http://localhost:9090"
info "  Tempo:      http://localhost:3200"
info ""
info "Next steps:"
info "  1. Wait ~10 min for baseline telemetry"
info "  2. Record the service names listed above (needed for tool config)"
info "  3. Run: python3 infra/seed_failures.py"
info "  4. Run: python3 infra/build_benchmarks.py"
