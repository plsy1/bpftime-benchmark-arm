#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

arch="$(uname -m)"
if [[ "$arch" != "x86_64" && "$arch" != "amd64" ]]; then
  echo "This smoke test is intended for x64, got: $arch" >&2
  echo "Use it on a GitHub ubuntu-24.04/x64 runner or an x86_64 machine." >&2
  exit 2
fi

export SYSCOUNT_NGINX_NUM_RUNS="${SYSCOUNT_NGINX_NUM_RUNS:-2}"
export SYSCOUNT_NGINX_WRK_DURATION="${SYSCOUNT_NGINX_WRK_DURATION:-2}"
export SYSCOUNT_NGINX_WRK_TIMEOUT="${SYSCOUNT_NGINX_WRK_TIMEOUT:-8}"
export SYSCOUNT_NGINX_DURATION="${SYSCOUNT_NGINX_DURATION:-4}"
export SYSCOUNT_NGINX_TIMEOUT="${SYSCOUNT_NGINX_TIMEOUT:-8}"
export SYSCOUNT_NGINX_STARTUP_DELAY="${SYSCOUNT_NGINX_STARTUP_DELAY:-1}"
export SYSCOUNT_NGINX_ALLOW_MISSING_USERBPF="${SYSCOUNT_NGINX_ALLOW_MISSING_USERBPF:-1}"

before="$(mktemp)"
after="$(mktemp)"
trap 'rm -f "$before" "$after"' EXIT

find benchmark/syscount-nginx -maxdepth 1 -name 'benchmark_results_*.json' -print | sort >"$before"

python3 benchmark/syscount-nginx/benchmark.py

find benchmark/syscount-nginx -maxdepth 1 -name 'benchmark_results_*.json' -print | sort >"$after"
result_file="$(comm -13 "$before" "$after" | tail -n 1)"
if [[ -z "$result_file" ]]; then
  result_file="$(find benchmark/syscount-nginx -maxdepth 1 -name 'benchmark_results_*.json' -print | sort | tail -n 1)"
fi

if [[ -z "$result_file" || ! -f "$result_file" ]]; then
  echo "No syscount result JSON was generated" >&2
  exit 1
fi

python3 - "$result_file" <<'PY'
import json
import statistics
import sys

path = sys.argv[1]
with open(path) as f:
    data = json.load(f)

results = data["results"]
trace = data["trace_observability"]
expected = [
    "native",
    "kernel_targeted",
    "kernel_untargeted",
    "userbpf_untargeted",
]
optional = ["userbpf_targeted"]

print(f"\nResult JSON: {path}")
print("\nThroughput:")
missing = []
for name in expected + optional:
    values = results.get(name, [])
    if not values:
        if name in expected:
            missing.append(name)
        print(f"  {name}: no data")
        continue
    print(
        f"  {name}: runs={len(values)} "
        f"avg={statistics.mean(values):.2f} "
        f"min={min(values):.2f} max={max(values):.2f}"
    )

print("\nTrace observability:")
trace_failures = []
for name in [
    "kernel_targeted",
    "kernel_untargeted",
    "userbpf_targeted",
    "userbpf_untargeted",
]:
    items = trace.get(name, [])
    ok = sum(1 for item in items if item.get("trace_success"))
    rows = [item.get("count_rows") for item in items]
    print(f"  {name}: trace_success={ok}/{len(items)} count_rows={rows}")
    if name in ("kernel_targeted", "kernel_untargeted", "userbpf_targeted") and ok != len(items):
        trace_failures.append(name)

if missing:
    print(f"\nMissing result groups: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)

if trace_failures:
    print(f"\nTrace failures: {', '.join(trace_failures)}", file=sys.stderr)
    sys.exit(1)

print("\nX64 syscount smoke passed.")
PY
