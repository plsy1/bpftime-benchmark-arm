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
