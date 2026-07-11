# Jetson Userspace Reproduction Container

This container freezes the benchmark userspace as closely as practical:

- Ubuntu 24.04 userspace
- nginx, OpenSSL, wrk, LLVM/Clang, libbpf-related build dependencies
- this bpftime benchmark tree and its built benchmark artifacts

It does **not** freeze the host kernel, CPU, cache hierarchy, scheduler, BTF/vmlinux availability, perf implementation, or thermal/power behavior. That is intentional: the container is meant to keep userspace mostly fixed while changing the host hardware/kernel.

## Build on Jetson

From the repository root:

```bash
docker build \
  --platform linux/arm64 \
  -f docker/jetson-repro/Dockerfile \
  -t bpftime-benchmark-jetson-userspace:20260709 \
  .
```

Export the image for another ARM64 machine:

```bash
docker save bpftime-benchmark-jetson-userspace:20260709 \
  | gzip > bpftime-benchmark-jetson-userspace-20260709.tar.gz
```

## Load on Another ARM64 Machine

```bash
gunzip -c bpftime-benchmark-jetson-userspace-20260709.tar.gz | docker load
```

Run smoke:

```bash
docker/jetson-repro/run-container.sh \
  ./run_bpftime_arm64_benchmarks.sh --mode smoke --skip-syscall
```

Run only `ssl-nginx`:

```bash
docker/jetson-repro/run-container.sh \
  ./run_bpftime_arm64_benchmarks.sh --only ssl-nginx --ssl-sizes 1kb
```

Run full `uprobe`:

```bash
docker/jetson-repro/run-container.sh \
  ./run_bpftime_arm64_benchmarks.sh --only uprobe
```

Results are written to:

```text
./docker-results/benchmark-results-arm64-container
```

## Runtime Requirements

The container must run with privileged access because kernel eBPF/perf/uprobe tests need host kernel facilities:

- `--privileged`
- host `/sys`
- host `/lib/modules`
- the default container network namespace is enough for nginx/wrk loopback tests

Do not use `--pid host` for these benchmark runs unless there is a specific
reason. The benchmark cleanup code terminates `nginx` and `sslsniff` by name,
so sharing the host PID namespace can accidentally kill host processes.

If the same image gives different `ssl-nginx` results on another ARM64 host, the remaining likely causes are host-side factors:

- CPU microarchitecture, frequency, IPC
- cache and memory hierarchy
- host kernel perf/uprobe implementation
- scheduler and IRQ behavior
- power/thermal policy
- BTF/vmlinux availability for other benchmarks

## Official Master No-BTF Image

`codex/jetson-no-btf-docker` preserves the official bpftime master commit plus
the Jetson compatibility build for hosts without `/sys/kernel/btf/vmlinux`.
It keeps `.BTF` for skeleton generation, removes `.BTF.ext`, and disables
CO-RE relocations in the local ARM64 vmlinux header. The corresponding ARM64
image is published by the `Build Jetson No-BTF Image` workflow.

Run the published image on Jetson without host networking:

```bash
IMAGE=ghcr.io/plsy1/bpftime:jetson-no-btf-arm64
NAME=bpftime-official-master-arm64-run

sudo docker pull "$IMAGE"
sudo docker rm -f "$NAME" 2>/dev/null || true
sudo docker run -d \
  --name "$NAME" \
  --privileged \
  -v /sys:/sys:ro \
  -v /lib/modules:/lib/modules:ro \
  "$IMAGE" sleep infinity
```

Run the unmodified official master full-size SSL-nginx driver in tmux:

```bash
LOG="$HOME/official-master-ssl-$(date +%Y%m%d_%H%M%S).log"
tmux new-session -d -s official-master-ssl-benchmark \
  "sudo docker exec $NAME bash -lc \
  'cd /bpftime && python3 benchmark/ssl-nginx/draw_figture.py' \
  2>&1 | tee '$LOG'"
```

Copy completed result files to the host:

```bash
OUT="$HOME/bpftime-official-master-results-$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUT"
sudo docker cp "$NAME:/bpftime/benchmark/ssl-nginx/size_benchmark_YYYYMMDD_HHMMSS.json" "$OUT/"
sudo docker cp "$NAME:/bpftime/benchmark/ssl-nginx/size_benchmark_YYYYMMDD_HHMMSS.txt" "$OUT/"
```
