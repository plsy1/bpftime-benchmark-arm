# bpftime ARM64 Benchmark Runner

This repository contains the bpftime benchmark code used for native ARM64 benchmark reproduction.

The main entry point is:

```bash
./run_bpftime_arm64_benchmarks.sh
```

The script can either run benchmarks from an existing local bpftime repository, or automatically clone this repository and run the benchmarks.

## Quick Start

On a native ARM64 Ubuntu machine, run:

```bash
wget https://raw.githubusercontent.com/plsy1/bpftime-benchmark-arm/main/run_bpftime_arm64_benchmarks.sh
chmod +x run_bpftime_arm64_benchmarks.sh
./run_bpftime_arm64_benchmarks.sh --clone
```

This will:

- clone `https://github.com/plsy1/bpftime-benchmark-arm.git`
- build bpftime and the benchmark targets
- run the main benchmark set
- skip MPK by default
- collect logs and results into a `.tar.gz` archive

## Run from an Existing Repository

If the repository already exists on the machine:

```bash
./run_bpftime_arm64_benchmarks.sh /path/to/bpftime-benchmark-arm
```

By default, this mode does not rebuild the project. It only runs benchmarks and collects results.

If a rebuild is needed:

```bash
./run_bpftime_arm64_benchmarks.sh --build /path/to/bpftime-benchmark-arm
```

The script auto-detects LLVM with `llvm-config --cmakedir` or versioned commands such as `llvm-config-15` and `llvm-config-21`.

If LLVM is installed in a custom path:

```bash
./run_bpftime_arm64_benchmarks.sh --build --llvm-dir /path/to/llvm/cmake /path/to/bpftime-benchmark-arm
```

## Smoke Test

To check whether the benchmark environment works before running the full benchmark:

```bash
./run_bpftime_arm64_benchmarks.sh --clone --mode smoke
```

Smoke mode uses:

- `uprobe --iter 1`
- `uprobe --test-iter 10000`
- `ssl-nginx` with `1kb` payload only

To smoke-test only the two benchmarks needed for the ARM64 run:

```bash
./run_bpftime_arm64_benchmarks.sh --clone --mode smoke --skip-syscall --skip-syscount
```

For the full uprobe + ssl-nginx run:

```bash
./run_bpftime_arm64_benchmarks.sh --clone --skip-syscall --skip-syscount
```

## GitHub Actions

The repository provides four manually triggered ARM64 workflows:

```text
Actions -> ARM64 Smoke -> Run workflow
Actions -> ARM64 Uprobe Full -> Run workflow
Actions -> ARM64 Syscount-nginx Full -> Run workflow
Actions -> ARM64 SSL-nginx Full -> Run workflow
```

Workflow defaults:

```text
ARM64 Smoke           uprobe_test_iter=10000, ssl_sizes=1kb
ARM64 Uprobe Full     uprobe_iter=10, uprobe_test_iter=100000
ARM64 Syscount-nginx Full
ARM64 SSL-nginx Full  ssl_sizes=16b,1kb,2kb,4kb,16kb,32kb,64kb,128kb,256kb
```

Each workflow uploads a result artifact containing the output directory, logs, copied benchmark result files, and a `.tar.gz` archive.

## Benchmark Selection

Run only one benchmark:

```bash
./run_bpftime_arm64_benchmarks.sh --clone --only uprobe
./run_bpftime_arm64_benchmarks.sh --clone --only syscall
./run_bpftime_arm64_benchmarks.sh --clone --only syscount-nginx
./run_bpftime_arm64_benchmarks.sh --clone --only ssl-nginx
```

Skip selected benchmarks:

```bash
./run_bpftime_arm64_benchmarks.sh --clone --skip-syscall
./run_bpftime_arm64_benchmarks.sh --clone --skip-uprobe --skip-syscount
```

MPK is skipped by default. To run it explicitly:

```bash
./run_bpftime_arm64_benchmarks.sh --clone --run-mpk
```

## ssl-nginx Payload Sizes

By default, `ssl-nginx` runs 9 payload sizes:

```text
16b,1kb,2kb,4kb,16kb,32kb,64kb,128kb,256kb
```

To run only selected sizes:

```bash
./run_bpftime_arm64_benchmarks.sh --clone --only ssl-nginx --ssl-sizes 1kb,16kb,128kb,256kb
```

The `ssl-nginx` benchmark script in this repository includes reliability fixes:

- waits for bpftime-instrumented nginx readiness
- records `wrk` return code and stderr
- supports bpftime retries
- supports `SSL_NGINX_SIZES`
- avoids treating empty `wrk` output as a valid sample

## Output

The script creates an output directory under the repository:

```text
benchmark-results-arm64-YYYYmmdd-HHMMSS/
```

It also creates an archive:

```text
benchmark-results-arm64-YYYYmmdd-HHMMSS.tar.gz
```

Please send back the `.tar.gz` archive after the run.

The output includes:

- `run.log`
- system and git information
- tool availability
- stdout/stderr logs for each benchmark
- return code for each benchmark
- copied benchmark result files such as `.json`, `.txt`, `.md`, `.png`, and `.log`

## Useful Options

```text
--clone                   Clone/pull the benchmark repository before running.
--repo-url URL            Repository URL used by --clone.
--branch BRANCH           Git branch used by --clone.
--workdir DIR             Parent directory for cloned repo.
--build                   Build bpftime and benchmark targets before running.
--no-build                Do not build; only run benchmarks.
--mode MODE               full or smoke.
--uprobe-iter N           Number of uprobe outer iterations.
--uprobe-test-iter N      Inner iterations for uprobe benchmark/test.
--ssl-sizes LIST          Comma-separated ssl-nginx sizes.
--only NAME               Run only one benchmark.
--skip-uprobe             Skip uprobe benchmark.
--skip-syscall            Skip syscall benchmark.
--skip-syscount           Skip syscount-nginx benchmark.
--skip-ssl-nginx          Skip ssl-nginx benchmark.
--run-mpk                 Run MPK benchmark.
--output-dir DIR          Output directory.
--llvm-dir DIR            LLVM CMake directory used by CMake.
-h, --help                Show help.
```

Full help:

```bash
./run_bpftime_arm64_benchmarks.sh --help
```

## Notes

- The script is intended for native ARM64 Ubuntu.
- MPK is skipped by default.
- `syscall` bpftime userspace tracing may fail on AArch64 if the syscall trampoline support is still incomplete.
- The script continues after individual benchmark failures and still collects logs.
- If `--clone` is used, build is enabled by default. Use `--no-build` to disable it.
- The repository contains compatibility changes for newer LLVM/GCC toolchains. The runner script does not require LLVM 15 specifically; it auto-detects the installed LLVM CMake directory.
