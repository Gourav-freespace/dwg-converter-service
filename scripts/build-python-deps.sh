#!/usr/bin/env bash
# Vendors the Python engine's dependencies (ezdxf, numpy) for the Lambda.
#
# Downloads manylinux x86_64 wheels (matching the python3.11 Lambda runtime)
# into python/vendor/, which handler.py puts on sys.path. Run once before
# the first deploy and after any requirements change. Works from macOS: it
# downloads prebuilt wheels, nothing is compiled.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="$REPO_ROOT/python/vendor"

rm -rf "$VENDOR"
mkdir -p "$VENDOR"

python3 -m pip install \
  --target "$VENDOR" \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  -r "$REPO_ROOT/python/requirements.txt"

# Trim what Lambda never needs.
find "$VENDOR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$VENDOR" -type d -name "tests" -path "*numpy*" -exec rm -rf {} + 2>/dev/null || true

echo
echo "Done: $VENDOR"
du -sh "$VENDOR"
