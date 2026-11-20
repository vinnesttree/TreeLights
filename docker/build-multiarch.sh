#!/bin/sh
# Build the TreeLights image for amd64 and arm64 (e.g. a Raspberry Pi 4 or 5) with
# docker buildx (architecture §6.1). Run from the repository root.
#
#   docker/build-multiarch.sh                     # build both, keep in the build cache
#   docker/build-multiarch.sh --push REGISTRY/treelights:0.1.0
#
# Building for the other architecture needs QEMU emulation once per host:
#   docker run --privileged --rm tonistiigi/binfmt --install arm64,amd64
# On the Raspberry Pi itself, plain `docker compose -f docker/docker-compose.yml up -d --build`
# builds the arm64 image natively; every Python dependency has a prebuilt aarch64 wheel.
set -e
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
if [ "$1" = "--push" ]; then
  [ -n "$2" ] || { echo "usage: $0 [--push IMAGE:TAG]" >&2; exit 2; }
  exec docker buildx build --platform "$PLATFORMS" -f docker/Dockerfile -t "$2" --push .
fi
exec docker buildx build --platform "$PLATFORMS" -f docker/Dockerfile -t treelights:0.1.0 .
