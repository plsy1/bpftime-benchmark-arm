# Build & Install Guide

This is a fork of [eunomia-bpf/bpftime](https://github.com/eunomia-bpf/bpftime) (tag `v0.2.0`) with compatibility patches for modern toolchains (Ubuntu 26.04, GCC 15, LLVM 21).

The source-code patches are applied **automatically** during CMake configure — no manual edits needed.

---

## System Requirements

| Component | Version tested |
|-----------|---------------|
| OS        | Ubuntu 26.04 (Noble/Oracular) |
| GCC       | 15.x |
| Clang/LLVM | 21.x |
| CMake     | 3.16+ |
| Python    | 3.12+ |

---

## 1. Install System Dependencies

```bash
sudo apt update && sudo apt install -y \
  git cmake ninja-build \
  gcc g++ clang llvm lld \
  libelf-dev \
  libboost-all-dev \
  libpcre2-dev \
  libfuse-dev \
  libcrypt-dev \
  python3 python3-pip \
  zlib1g-dev \
  libssl-dev \
  pkg-config
```

> **Note**: On Ubuntu 26.04, `libpcre3-dev` is no longer available — use `libpcre2-dev` instead.
> `libcrypt-dev` is also needed since glibc 2.39+ removed crypt from the core library.

---

## 2. Install Python Dependencies

```bash
pip install -r benchmark/requirements.txt --break-system-packages
```

---

## 3. Clone (with submodules)

```bash
git clone --recursive https://github.com/plsy1/bpftime-benchmark.git
cd bpftime-benchmark
```

If you already cloned without `--recursive`:

```bash
git submodule update --init --recursive
```

---

## 4. Build

### Benchmark build (recommended)

Builds the runtime + all benchmark targets:

```bash
make benchmark
```

### Release build

```bash
make release
```

### Other targets

```bash
make help   # list all available targets
```

---

## 5. Run Benchmarks

```bash
# uprobe micro-benchmark
python3 benchmark/uprobe/benchmark.py

# syscall micro-benchmark
sudo build/tools/bpftimetool/bpftimetool remove
python3 benchmark/syscall/benchmark.py

# nginx system benchmark
sudo build/tools/bpftimetool/bpftimetool remove
python3 benchmark/syscount-nginx/benchmark.py
```

---

## Compatibility Notes

The following patches are applied automatically at CMake configure time and **do not require any manual changes**:

| Patch | What it fixes |
|-------|--------------|
| `cmake/patch_bpftool_libbpf.py` | GCC 15 `-Werror=discarded-qualifiers` in bpftool's bundled libbpf; `bpf_stream_vprintk` conflict with modern `vmlinux.h` |
| `cmake/llvm_jit_compat.patch` | LLVM 21 API: `setTargetTriple()` now requires `llvm::Triple`; adds `EXTRA_CFLAGS=-Wno-error` for bundled libbpf in CLI |
