#!/usr/bin/env bash
# Regression coverage for scripts/validate-manifest-schema.sh.
# Ensures the validator stays aligned with current manifest schema semantics.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALIDATOR="$ROOT_DIR/scripts/validate-manifest-schema.sh"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

write_manifest() {
  local service_dir="$1"
  local gpu_backend="$2"
  mkdir -p "$service_dir"
  cat > "$service_dir/manifest.yaml" <<YAML
schema_version: dream.services.v1
service:
  id: $(basename "$service_dir")
  name: Test Service
  port: 0
  health: ""
  type: docker
  category: optional
  gpu_backends: [$gpu_backend]
features:
  - id: $(basename "$service_dir")
    name: Test Feature
    description: Test feature
    icon: Box
    category: testing
    requirements:
      services: [$(basename "$service_dir")]
    priority: 1
    gpu_backends: [$gpu_backend]
YAML
}

assert_success() {
  local label="$1"
  shift
  if ! "$@" >/tmp/validate-manifest-schema-success.log 2>&1; then
    echo "[FAIL] $label" >&2
    cat /tmp/validate-manifest-schema-success.log >&2
    exit 1
  fi
  echo "[PASS] $label"
}

assert_failure() {
  local label="$1"
  shift
  if "$@" >/tmp/validate-manifest-schema-failure.log 2>&1; then
    echo "[FAIL] $label unexpectedly succeeded" >&2
    cat /tmp/validate-manifest-schema-failure.log >&2
    exit 1
  fi
  echo "[PASS] $label"
}

assert_success "current bundled and library manifests validate" bash "$VALIDATOR"

HOST_NETWORK_DIR="$TMP_DIR/hostnet/tailscale-like"
mkdir -p "$HOST_NETWORK_DIR"
cat > "$HOST_NETWORK_DIR/manifest.yaml" <<'YAML'
schema_version: dream.services.v1
service:
  id: tailscale-like
  name: Tailscale Like
  host_network: true
  port: 0
  type: docker
  category: optional
  gpu_backends: [none]
features:
  - id: tailscale-like
    name: Tailscale Like
    description: Host network service with compose/native health
    icon: Globe
    category: testing
    requirements:
      services: [tailscale-like]
    priority: 1
    gpu_backends: [none]
YAML
assert_success "host_network service may omit service.health and use gpu_backends none" \
  env DREAM_MANIFEST_DIRS="$TMP_DIR/hostnet" bash "$VALIDATOR"

HOST_SYSTEMD_DIR="$TMP_DIR/hostsystemd/opencode-like"
mkdir -p "$HOST_SYSTEMD_DIR"
cat > "$HOST_SYSTEMD_DIR/manifest.yaml" <<'YAML'
schema_version: dream.services.v1
service:
  id: opencode-like
  name: OpenCode Like
  port: 3003
  health: /
  type: host-systemd
  category: optional
  gpu_backends: [all]
features:
  - id: opencode-like
    name: OpenCode Like
    description: Host systemd service
    icon: Code
    category: testing
    requirements:
      services: [opencode-like]
    priority: 1
    gpu_backends: [all]
YAML
assert_success "host-systemd service type validates" \
  env DREAM_MANIFEST_DIRS="$TMP_DIR/hostsystemd" bash "$VALIDATOR"

write_manifest "$TMP_DIR/bad/service-bad" "quantum"
assert_failure "invalid service gpu backend is rejected" \
  env DREAM_MANIFEST_DIRS="$TMP_DIR/bad" bash "$VALIDATOR"

write_manifest "$TMP_DIR/bad-feature/service-bad-feature" "all"
python3 - "$TMP_DIR/bad-feature/service-bad-feature/manifest.yaml" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
lines = path.read_text().splitlines()
seen_gpu_backends = 0
for idx, line in enumerate(lines):
    if line.strip() == "gpu_backends: [all]":
        seen_gpu_backends += 1
        if seen_gpu_backends == 2:
            lines[idx] = line.replace("[all]", "[quantum]")
            break
else:
    raise SystemExit("feature gpu_backends line not found")
path.write_text("\n".join(lines) + "\n")
PY
assert_failure "invalid feature gpu backend is rejected" \
  env DREAM_MANIFEST_DIRS="$TMP_DIR/bad-feature" bash "$VALIDATOR"

echo "validate-manifest-schema regression tests passed"
