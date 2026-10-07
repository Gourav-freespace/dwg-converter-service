#!/usr/bin/env bash
# Builds the dwg2dxf binary (LibreDWG) for the Lambda layer.
#
# Compiles LibreDWG inside an amazonlinux:2023 container for linux/amd64
# and writes the static binary to layers/dwg-converter/bin/dwg2dxf.
#
# Requires Docker. Run from anywhere; paths resolve from the repo root.
#
# Usage:
#   ./scripts/build-dwg-layer.sh            # build 0.14 (default)
#   LIBREDWG_VERSION=0.14 ./scripts/build-dwg-layer.sh

set -euo pipefail

VERSION="${LIBREDWG_VERSION:-0.14}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$REPO_ROOT/layers/dwg-converter/bin"
TARBALL_URL="https://github.com/LibreDWG/libredwg/releases/download/${VERSION}/libredwg-${VERSION}.tar.xz"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is required. Install Docker (or run this in CI)." >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

# Corporate TLS interception (e.g. Zscaler): set BUILD_CA_BUNDLE to a PEM
# file and the container trusts it before dnf/curl run.
CA_MOUNT=()
CA_SETUP=":"
if [ -n "${BUILD_CA_BUNDLE:-}" ]; then
  CA_MOUNT=(-v "$BUILD_CA_BUNDLE":/corp-ca.pem:ro)
  CA_SETUP="cp /corp-ca.pem /etc/pki/ca-trust/source/anchors/corp-ca.pem && update-ca-trust"
fi

echo "Building LibreDWG ${VERSION} dwg2dxf for linux/amd64 (Amazon Linux 2023)..."

docker run --rm --platform linux/amd64 \
  -v "$OUT_DIR":/out \
  "${CA_MOUNT[@]}" \
  amazonlinux:2023 \
  bash -euxc "
    ${CA_SETUP}
    dnf install -y -q gcc make tar xz gzip diffutils file
    cd /tmp
    curl -fsSL -o libredwg.tar.xz '${TARBALL_URL}'
    tar xf libredwg.tar.xz
    cd libredwg-${VERSION}
    # Static link so the layer needs no shared libraries.
    ./configure --disable-shared --disable-bindings --disable-python \
                --disable-dxf-tests --without-perl-install >/dev/null
    make -C src -j\"\$(nproc)\" >/dev/null
    make -C programs -j\"\$(nproc)\" dwg2dxf >/dev/null
    strip programs/dwg2dxf
    cp programs/dwg2dxf /out/dwg2dxf
    file /out/dwg2dxf
    /out/dwg2dxf --version || true
  "

echo
echo "Done: $OUT_DIR/dwg2dxf"
echo "Deploy with: npm run deploy (the serverless layer packages this file)."
