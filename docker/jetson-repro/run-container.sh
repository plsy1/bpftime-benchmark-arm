#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-bpftime-benchmark-jetson-userspace:20260709}"
OUT_DIR="${OUT_DIR:-$PWD/docker-results}"

mkdir -p "$OUT_DIR"

if [[ "$#" -eq 0 ]]; then
  set -- ./run_bpftime_arm64_benchmarks.sh --mode smoke --skip-syscall
fi

TTY_ARGS=()
if [[ -t 0 && -t 1 ]]; then
  TTY_ARGS=(-it)
fi

exec docker run --rm "${TTY_ARGS[@]}" \
  --name bpftime-benchmark-repro \
  --privileged \
  -v /sys:/sys \
  -v /lib/modules:/lib/modules:ro \
  -v "$OUT_DIR:/results" \
  -e SSL_NGINX_SSLSNIFF_ARGS="${SSL_NGINX_SSLSNIFF_ARGS:---no-gnutls --no-nss -c nginx}" \
  -e SYSCOUNT_NGINX_BIN="${SYSCOUNT_NGINX_BIN:-nginx}" \
  "$IMAGE" \
  "$@" --output-dir /results/benchmark-results-arm64-container
