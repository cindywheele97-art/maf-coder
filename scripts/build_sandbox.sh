#!/usr/bin/env bash
#
# Build the MAF-Coder Rust sandbox Docker image with persistent volumes.
#
# Usage:
#   bash scripts/build_sandbox.sh                    # default tag
#   bash scripts/build_sandbox.sh --tag myname:dev   # custom tag
#   bash scripts/build_sandbox.sh --no-cache         # force full rebuild
#   bash scripts/build_sandbox.sh --build-arg RUST_VERSION=1.90  # override (if Dockerfile uses ARG)
#
# Expected first-time build: 30-60 minutes (mostly cargo install of ~12 tools).
# Expected rebuilds: 1-5 minutes (Docker layer cache).
#
# The persistent volumes maf-cargo-cache / maf-target-cache / maf-sccache
# are critical for multi-day Rust task performance — see soul.md §5.3.
# Once created, they survive across container restarts and are mounted by
# every mission's sandbox container.

set -euo pipefail

TAG="maf-coder:rust-sandbox"
DOCKERFILE="${DOCKERFILE:-config/rust_sandbox.dockerfile}"
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag) TAG="$2"; shift 2 ;;
        --no-cache) EXTRA_ARGS+=("--no-cache"); shift ;;
        --build-arg) EXTRA_ARGS+=("--build-arg" "$2"); shift 2 ;;
        -h|--help)
            sed -n '2,18p' "$0"; exit 0 ;;
        *)
            echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found. Install Docker first." >&2
    exit 1
fi

if [[ ! -f "$DOCKERFILE" ]]; then
    echo "ERROR: Dockerfile not found: $DOCKERFILE" >&2
    echo "Override with: DOCKERFILE=path/to/Dockerfile bash $0" >&2
    exit 1
fi

# -- Persistent volumes (idempotent) -----------------------------------------

for vol in maf-cargo-cache maf-target-cache maf-sccache; do
    if ! docker volume inspect "$vol" >/dev/null 2>&1; then
        echo "Creating volume: $vol"
        docker volume create "$vol" >/dev/null
    else
        echo "Volume already exists: $vol"
    fi
done

# -- Build --------------------------------------------------------------------

echo
echo "Building $TAG from $DOCKERFILE..."
echo "(this can take 30-60 minutes on first run — go make tea)"
echo
docker build -t "$TAG" -f "$DOCKERFILE" "${EXTRA_ARGS[@]}" .

# -- Smoke check ---------------------------------------------------------------
# Verify the key tools that future Workers and Validators depend on actually
# exist and respond. If any of these fail, Phase B will not be able to start
# the relevant Workers cleanly.

echo
echo "Build complete. Sanity-checking installed tools..."
echo
docker run --rm "$TAG" bash -lc '
    set -e
    check() {
        local name="$1"; shift
        if out=$("$@" 2>&1 | head -1); then
            printf "  ✓ %-12s %s\n" "$name" "$out"
        else
            printf "  ✗ %-12s FAILED: %s\n" "$name" "$out"
            return 1
        fi
    }
    check rustc       rustc --version
    check cargo       cargo --version
    check clippy      cargo clippy --version
    check fmt         cargo fmt --version
    check audit       cargo audit --version
    check deny        cargo deny --version
    check geiger      cargo geiger --version
    check nextest     cargo nextest --version
    check wasm-pack   wasm-pack --version
    check sccache     sccache --version
    check gitleaks    gitleaks version
    check gh          gh --version
    check git         git --version
    check protoc      protoc --version
'

echo
echo "============================================================"
echo "Image $TAG ready. Next:"
echo "  1. Run the smoke test (host side, separate gate):"
echo "       python scripts/smoke_test.py"
echo "  2. Continue to Phase B (Orchestrator planner)."
echo "============================================================"
