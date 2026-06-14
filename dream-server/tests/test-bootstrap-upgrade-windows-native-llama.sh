#!/usr/bin/env bash
# Regression: Windows AMD llama-server fallback must hot-swap the host-native
# llama-server.exe after the background full-model download completes.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$ROOT_DIR/scripts/bootstrap-upgrade.sh"

fail() {
    echo "[FAIL] $*" >&2
    exit 1
}

pass() {
    echo "[PASS] $*"
}

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fakebin="$tmp/bin"
install_dir="$tmp/install"
trace="$tmp/powershell.trace"
mkdir -p \
    "$fakebin" \
    "$install_dir/data/hermes" \
    "$install_dir/data/models" \
    "$install_dir/config/llama-server" \
    "$install_dir/extensions/services/hermes" \
    "$install_dir/llama-server"

cat > "$fakebin/uname" <<'EOF_UNAME'
#!/usr/bin/env bash
printf 'MINGW64_NT-10.0\n'
EOF_UNAME
chmod +x "$fakebin/uname"

cat > "$fakebin/curl" <<'EOF_CURL'
#!/usr/bin/env bash
case " $* " in
  *" -sI "*)
    printf 'HTTP/2 200\r\ncontent-length: 11\r\n\r\n'
    exit 0
    ;;
esac
exit 22
EOF_CURL
chmod +x "$fakebin/curl"

cat > "$fakebin/powershell.exe" <<'EOF_PS'
#!/usr/bin/env bash
set -euo pipefail
: "${DREAM_WIN_PID_FILE:?}"
: "${DREAM_WIN_LLAMA_EXE:?}"
: "${DREAM_WIN_MODEL_PATH:?}"
: "${DREAM_WIN_ROLLBACK_MODEL_PATH:?}"
: "${DREAM_WIN_LLAMA_PORT:?}"
: "${DREAM_WIN_CTX_SIZE:?}"
{
  printf 'exe=%s\n' "$DREAM_WIN_LLAMA_EXE"
  printf 'model=%s\n' "$DREAM_WIN_MODEL_PATH"
  printf 'rollback=%s\n' "$DREAM_WIN_ROLLBACK_MODEL_PATH"
  printf 'port=%s\n' "$DREAM_WIN_LLAMA_PORT"
  printf 'ctx=%s\n' "$DREAM_WIN_CTX_SIZE"
} >> "${DREAM_FAKE_PS_TRACE:?}"
mkdir -p "$(dirname "$DREAM_WIN_PID_FILE")"
printf '4242\n' > "$DREAM_WIN_PID_FILE"
exit 0
EOF_PS
chmod +x "$fakebin/powershell.exe"

cat > "$install_dir/.env" <<'EOF_ENV'
DREAM_MODE=local
GPU_BACKEND=amd
LLM_BACKEND=llama-server
LLM_API_URL=http://host.docker.internal:8080
LLM_API_BASE_PATH=/v1
AMD_INFERENCE_RUNTIME=llama-server
AMD_INFERENCE_BACKEND=vulkan
AMD_INFERENCE_LOCATION=host
AMD_INFERENCE_PORT=8080
AMD_INFERENCE_RUNTIME_MODE=windows-llama-server-fallback
AMD_INFERENCE_MANAGED=true
BIND_ADDRESS=127.0.0.1
GGUF_FILE=Bootstrap.gguf
LLM_MODEL=bootstrap-model
MAX_CONTEXT=8192
CTX_SIZE=8192
HERMES_LLM_BASE_URL=http://host.docker.internal:8080/v1
EOF_ENV

cat > "$install_dir/extensions/services/hermes/cli-config.yaml.template" <<'EOF_HERMES'
model:
  default: "Bootstrap.gguf"
  provider: "custom"
  base_url: "http://host.docker.internal:8080/v1"
  context_length: 8192
EOF_HERMES

cat > "$install_dir/data/hermes/config.yaml" <<'EOF_HERMES_LIVE'
model:
  default: "Full.gguf"
  provider: "custom"
  base_url: "http://stale.invalid/v1"
  context_length: 8192
auxiliary:
  compression:
    context_length: 8192
EOF_HERMES_LIVE

printf 'bootstrap\n' > "$install_dir/data/models/Bootstrap.gguf"
printf 'full-model\n' > "$install_dir/data/models/Full.gguf"
printf '#!/usr/bin/env bash\nexit 0\n' > "$install_dir/llama-server/llama-server.exe"
chmod +x "$install_dir/llama-server/llama-server.exe"
printf '1111\n' > "$install_dir/data/llama-server.pid"

PATH="$fakebin:$PATH" DREAM_FAKE_PS_TRACE="$trace" bash "$TARGET" \
    "$install_dir" \
    "Full.gguf" \
    "https://example.invalid/Full.gguf" \
    "" \
    "full-model" \
    "32768" \
    "Bootstrap.gguf" \
    > "$tmp/bootstrap.log" 2>&1

grep -q 'Restarting native Windows llama-server with full model' "$tmp/bootstrap.log" \
    || fail "bootstrap-upgrade should restart the native Windows llama-server fallback"
grep -q 'SUCCESS: native Windows llama-server running with Full.gguf' "$tmp/bootstrap.log" \
    || fail "bootstrap-upgrade should mark the native Windows llama-server swap verified"
grep -q 'model=.*Full.gguf$' "$trace" \
    || fail "PowerShell restart should receive the full GGUF path"
grep -q 'rollback=.*Bootstrap.gguf$' "$trace" \
    || fail "PowerShell restart should receive the bootstrap rollback path"
grep -q 'port=8080' "$trace" \
    || fail "PowerShell restart should target the AMD inference port"
grep -q 'ctx=32768' "$trace" \
    || fail "PowerShell restart should target the full-model context"
grep -q '^GGUF_FILE=Full.gguf$' "$install_dir/.env" \
    || fail "bootstrap-upgrade should promote GGUF_FILE after verified restart"
grep -q '^LLM_MODEL=full-model$' "$install_dir/.env" \
    || fail "bootstrap-upgrade should promote LLM_MODEL after verified restart"
grep -q 'default: "Full.gguf"' "$install_dir/extensions/services/hermes/cli-config.yaml.template" \
    || fail "Hermes should use the bare GGUF id for Windows llama-server fallback"
! grep -q 'extra.Full.gguf' "$install_dir/extensions/services/hermes/cli-config.yaml.template" \
    || fail "Hermes should not use Lemonade extra.* model ids for Windows llama-server fallback"
grep -q 'default: "Full.gguf"' "$install_dir/data/hermes/config.yaml" \
    || fail "Hermes live config should keep the bare full-model id"
grep -q 'base_url: "http://host.docker.internal:8080/v1"' "$install_dir/data/hermes/config.yaml" \
    || fail "Hermes live config should preserve the generated llama-server route"
grep -q '^  context_length: 32768$' "$install_dir/data/hermes/config.yaml" \
    || fail "Hermes live config should use the full-model context"
grep -q '^    context_length: 32768$' "$install_dir/data/hermes/config.yaml" \
    || fail "Hermes auxiliary compression context should use the full-model context"
[[ ! -f "$install_dir/data/models/Bootstrap.gguf" ]] \
    || fail "bootstrap model should be removed after verified native Windows swap"
grep -q '"status": "complete"' "$install_dir/data/bootstrap-status.json" \
    || fail "bootstrap status should finish complete"

pass "Windows native llama-server bootstrap swap is verified"
