#!/usr/bin/env bash
set -u -o pipefail

# bpftime native ARM64 benchmark runner
#
# Usage:
#   ./run_bpftime_arm64_benchmarks.sh [options] [repo_path]
#
# Common examples:
#   ./run_bpftime_arm64_benchmarks.sh /path/to/bpftime-benchmark-arm
#   ./run_bpftime_arm64_benchmarks.sh --clone --build
#   ./run_bpftime_arm64_benchmarks.sh --build /path/to/bpftime-benchmark-arm
#   ./run_bpftime_arm64_benchmarks.sh --mode smoke /path/to/bpftime-benchmark-arm
#   ./run_bpftime_arm64_benchmarks.sh --only ssl-nginx --ssl-sizes 1kb,16kb,128kb /path/to/bpftime-benchmark-arm
#
# Notes:
# - MPK is skipped.
# - The syscall bpftime userspace case may fail on AArch64 if the
#   text_segment_transformer syscall trampoline is not implemented.
# - The script continues after individual benchmark failures and collects logs.

usage() {
  cat <<'EOF'
bpftime native ARM64 benchmark runner

Usage:
  ./run_bpftime_arm64_benchmarks.sh [options] [repo_path]

Arguments:
  repo_path                 Path to the bpftime repository. Default: current directory.

Options:
  --build                   Build bpftime and benchmark targets before running.
  --build-only              Build bpftime and benchmark targets, then skip benchmark execution.
  --no-build                Do not build; only run benchmarks. This is the default.
  --clone                   Clone/pull the benchmark repository before running.
                            With --clone, build is enabled by default unless --no-build is set.
                            Default repo: https://github.com/plsy1/bpftime-benchmark-arm.git
  --repo-url URL            Repository URL used by --clone.
  --branch BRANCH           Git branch used by --clone. Default: repository default branch.
  --workdir DIR             Parent directory for cloned repo. Default: ./bpftime-arm64-run.
  --mode MODE               full or smoke. Default: full.
                            smoke uses uprobe --iter 1, uprobe --test-iter 10000,
                            ssl-nginx 1kb only, and short syscount-nginx runs.
  --uprobe-iter N           Number of uprobe outer iterations. Default: 10 in full, 1 in smoke.
  --uprobe-test-iter N      Inner iterations passed to benchmark/uprobe/benchmark.py.
                            Default: 100000 in full, 10000 in smoke.
  --ssl-sizes LIST          Comma-separated ssl-nginx sizes.
                            Default: 16b,1kb,2kb,4kb,16kb,32kb,64kb,128kb,256kb.
  --ssl-nginx-sslsniff-args ARGS
                            Extra arguments passed to sslsniff. On AArch64 the default
                            is "--no-gnutls --no-nss -c nginx" unless
                            SSL_NGINX_SSLSNIFF_ARGS is already set.
  --strict-ssl-nginx-trace-errors
                            Treat sslsniff trace warnings, such as perf polling
                            errors after SSL events were observed, as benchmark
                            failures. Default: off.
  --only NAME               Run only one benchmark: uprobe, syscall, syscount, ssl-nginx, mpk.
  --skip-uprobe             Skip uprobe benchmark.
  --skip-syscall            Skip syscall benchmark.
  --skip-syscount           Skip syscount-nginx benchmark.
  --skip-ssl-nginx          Skip ssl-nginx benchmark.
  --run-mpk                 Run MPK benchmark. Default: skipped.
  --output-dir DIR          Output directory. Default: repo/benchmark-results-arm64-TIMESTAMP.
  --llvm-dir DIR            LLVM CMake directory. If not set, auto-detect with llvm-config.
  --install-deps            Install native ARM64 Ubuntu dependencies with apt-get before running.
                            Default: only check dependencies and print warnings.
  --no-check-deps           Skip dependency checks.
  --keep-previous-results   Keep previous unpacked benchmark result directories and generated
                            benchmark artifacts. Default: clean them before each run while
                            preserving existing .tar.gz archives.
  -h, --help                Show this help message.

Examples:
  ./run_bpftime_arm64_benchmarks.sh /path/to/bpftime-benchmark-arm
  ./run_bpftime_arm64_benchmarks.sh --clone --build
  ./run_bpftime_arm64_benchmarks.sh --clone --branch main --build
  ./run_bpftime_arm64_benchmarks.sh --build /path/to/bpftime-benchmark-arm
  ./run_bpftime_arm64_benchmarks.sh --mode smoke /path/to/bpftime-benchmark-arm
  ./run_bpftime_arm64_benchmarks.sh --only ssl-nginx --ssl-sizes 1kb,16kb,128kb /path/to/bpftime-benchmark-arm

Notes:
  - MPK is skipped by default.
  - The syscall bpftime userspace case may fail on AArch64 if the
    text_segment_transformer syscall trampoline is not implemented.
  - The script continues after individual benchmark failures and collects logs.
EOF
}

REPO=""
TS="$(date +%Y%m%d-%H%M%S)"
OUT=""
DO_CLONE=0
REPO_URL="https://github.com/plsy1/bpftime-benchmark-arm.git"
BRANCH=""
WORKDIR="$PWD/bpftime-arm64-run"

MODE="${MODE:-full}"              # full or smoke
if [[ -v RUN_BUILD ]]; then
  RUN_BUILD_EXPLICIT=1
else
  RUN_BUILD_EXPLICIT=0
fi
RUN_BUILD="${RUN_BUILD:-0}"       # 1: build before running, 0: skip build
BUILD_ONLY="${BUILD_ONLY:-0}"     # 1: build selected targets and skip benchmark execution
RUN_UPROBE="${RUN_UPROBE:-1}"
RUN_SYSCALL="${RUN_SYSCALL:-1}"
RUN_SYSCOUNT="${RUN_SYSCOUNT:-1}"
RUN_SSL_NGINX="${RUN_SSL_NGINX:-1}"
RUN_MPK="${RUN_MPK:-0}"
USER_SSL_NGINX_SIZES="${SSL_NGINX_SIZES:-}"
USER_SSL_NGINX_SSLSNIFF_ARGS="${SSL_NGINX_SSLSNIFF_ARGS-}"
SSL_NGINX_STRICT_TRACE_ERRORS="${SSL_NGINX_STRICT_TRACE_ERRORS:-0}"
SSL_NGINX_SSLSNIFF_ARGS_IS_SET=0
if [[ -v SSL_NGINX_SSLSNIFF_ARGS ]]; then
  SSL_NGINX_SSLSNIFF_ARGS_IS_SET=1
fi
USER_UPROBE_ITER="${UPROBE_ITER:-}"
USER_UPROBE_TEST_ITER="${UPROBE_TEST_ITER:-}"
MPK_ITER="${MPK_ITER:-10}"
USER_LLVM_DIR="${LLVM_DIR:-}"
INSTALL_DEPS="${INSTALL_DEPS:-0}"
CHECK_DEPS="${CHECK_DEPS:-1}"
FAILURES=0
ONLY_BENCH=""
AUTO_SKIPPED_ARM64_SYSCALL=0
KEEP_PREVIOUS_RESULTS="${KEEP_PREVIOUS_RESULTS:-0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build)
      RUN_BUILD=1
      RUN_BUILD_EXPLICIT=1
      shift
      ;;
    --build-only)
      RUN_BUILD=1
      RUN_BUILD_EXPLICIT=1
      BUILD_ONLY=1
      shift
      ;;
    --no-build)
      RUN_BUILD=0
      RUN_BUILD_EXPLICIT=1
      shift
      ;;
    --clone)
      DO_CLONE=1
      shift
      ;;
    --repo-url)
      REPO_URL="${2:?missing value for --repo-url}"
      shift 2
      ;;
    --repo-url=*)
      REPO_URL="${1#*=}"
      shift
      ;;
    --branch)
      BRANCH="${2:?missing value for --branch}"
      shift 2
      ;;
    --branch=*)
      BRANCH="${1#*=}"
      shift
      ;;
    --workdir)
      WORKDIR="${2:?missing value for --workdir}"
      shift 2
      ;;
    --workdir=*)
      WORKDIR="${1#*=}"
      shift
      ;;
    --mode)
      MODE="${2:?missing value for --mode}"
      shift 2
      ;;
    --mode=*)
      MODE="${1#*=}"
      shift
      ;;
    --uprobe-iter)
      USER_UPROBE_ITER="${2:?missing value for --uprobe-iter}"
      shift 2
      ;;
    --uprobe-iter=*)
      USER_UPROBE_ITER="${1#*=}"
      shift
      ;;
    --uprobe-test-iter)
      USER_UPROBE_TEST_ITER="${2:?missing value for --uprobe-test-iter}"
      shift 2
      ;;
    --uprobe-test-iter=*)
      USER_UPROBE_TEST_ITER="${1#*=}"
      shift
      ;;
    --ssl-sizes)
      USER_SSL_NGINX_SIZES="${2:?missing value for --ssl-sizes}"
      shift 2
      ;;
    --ssl-sizes=*)
      USER_SSL_NGINX_SIZES="${1#*=}"
      shift
      ;;
    --ssl-nginx-sslsniff-args)
      USER_SSL_NGINX_SSLSNIFF_ARGS="${2:?missing value for --ssl-nginx-sslsniff-args}"
      SSL_NGINX_SSLSNIFF_ARGS_IS_SET=1
      shift 2
      ;;
    --ssl-nginx-sslsniff-args=*)
      USER_SSL_NGINX_SSLSNIFF_ARGS="${1#*=}"
      SSL_NGINX_SSLSNIFF_ARGS_IS_SET=1
      shift
      ;;
    --only)
      only="${2:?missing value for --only}"
      ONLY_BENCH="$only"
      RUN_UPROBE=0
      RUN_SYSCALL=0
      RUN_SYSCOUNT=0
      RUN_SSL_NGINX=0
      RUN_MPK=0
      case "$only" in
        uprobe) RUN_UPROBE=1 ;;
        syscall) RUN_SYSCALL=1 ;;
        syscount|syscount-nginx) RUN_SYSCOUNT=1 ;;
        ssl|ssl-nginx) RUN_SSL_NGINX=1 ;;
        mpk) RUN_MPK=1 ;;
        *) echo "Unknown benchmark for --only: $only" >&2; exit 2 ;;
      esac
      shift 2
      ;;
    --only=*)
      only="${1#*=}"
      ONLY_BENCH="$only"
      RUN_UPROBE=0
      RUN_SYSCALL=0
      RUN_SYSCOUNT=0
      RUN_SSL_NGINX=0
      RUN_MPK=0
      case "$only" in
        uprobe) RUN_UPROBE=1 ;;
        syscall) RUN_SYSCALL=1 ;;
        syscount|syscount-nginx) RUN_SYSCOUNT=1 ;;
        ssl|ssl-nginx) RUN_SSL_NGINX=1 ;;
        mpk) RUN_MPK=1 ;;
        *) echo "Unknown benchmark for --only: $only" >&2; exit 2 ;;
      esac
      shift
      ;;
    --skip-uprobe)
      RUN_UPROBE=0
      shift
      ;;
    --skip-syscall)
      RUN_SYSCALL=0
      shift
      ;;
    --skip-syscount|--skip-syscount-nginx)
      RUN_SYSCOUNT=0
      shift
      ;;
    --skip-ssl-nginx|--skip-ssl)
      RUN_SSL_NGINX=0
      shift
      ;;
    --strict-ssl-nginx-trace-errors)
      SSL_NGINX_STRICT_TRACE_ERRORS=1
      shift
      ;;
    --run-mpk)
      RUN_MPK=1
      shift
      ;;
    --output-dir)
      OUT="${2:?missing value for --output-dir}"
      shift 2
      ;;
    --output-dir=*)
      OUT="${1#*=}"
      shift
      ;;
    --llvm-dir)
      USER_LLVM_DIR="${2:?missing value for --llvm-dir}"
      shift 2
      ;;
    --llvm-dir=*)
      USER_LLVM_DIR="${1#*=}"
      shift
      ;;
    --install-deps)
      INSTALL_DEPS=1
      CHECK_DEPS=1
      shift
      ;;
    --no-check-deps)
      CHECK_DEPS=0
      shift
      ;;
    --keep-previous-results)
      KEEP_PREVIOUS_RESULTS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [[ -z "$REPO" ]]; then
        REPO="$1"
      else
        echo "Unexpected extra argument: $1" >&2
        usage >&2
        exit 2
      fi
      shift
      ;;
  esac
done

REPO="${REPO:-$PWD}"
if [[ "$DO_CLONE" == "1" ]]; then
  if [[ "$RUN_BUILD_EXPLICIT" == "0" ]]; then
    RUN_BUILD=1
  fi

  WORKDIR="$(realpath -m "$WORKDIR")"
  mkdir -p "$WORKDIR"
  repo_name="$(basename "$REPO_URL")"
  repo_name="${repo_name%.git}"
  REPO="$WORKDIR/$repo_name"

  if [[ -d "$REPO/.git" ]]; then
    echo "[$(date '+%F %T')] Existing repository found: $REPO"
    (
      cd "$REPO"
      git fetch --all --prune
      if [[ -n "$BRANCH" ]]; then
        git checkout "$BRANCH"
        git pull --ff-only origin "$BRANCH" || true
      else
        git pull --ff-only || true
      fi
    )
  else
    if [[ -n "$BRANCH" ]]; then
      git clone --branch "$BRANCH" "$REPO_URL" "$REPO"
    else
      git clone "$REPO_URL" "$REPO"
    fi
  fi
fi

REPO="$(realpath "$REPO")"
OUT="${OUT:-$REPO/benchmark-results-arm64-$TS}"
OUT="$(realpath -m "$OUT")"
RUN_MARKER="$OUT/.benchmark-start"

case "$MODE" in
  full|smoke) ;;
  *) echo "Invalid --mode: $MODE (expected full or smoke)" >&2; exit 2 ;;
esac

if [[ "$MODE" == "smoke" ]]; then
  UPROBE_ITER="${USER_UPROBE_ITER:-1}"
  UPROBE_TEST_ITER="${USER_UPROBE_TEST_ITER:-10000}"
  SSL_NGINX_SIZES="${USER_SSL_NGINX_SIZES:-1kb}"
  export SYSCOUNT_NGINX_NUM_RUNS="${SYSCOUNT_NGINX_NUM_RUNS:-1}"
  export SYSCOUNT_NGINX_WRK_DURATION="${SYSCOUNT_NGINX_WRK_DURATION:-1}"
  export SYSCOUNT_NGINX_DURATION="${SYSCOUNT_NGINX_DURATION:-4}"
  export SYSCOUNT_NGINX_WRK_TIMEOUT="${SYSCOUNT_NGINX_WRK_TIMEOUT:-5}"
  export SYSCOUNT_NGINX_TIMEOUT="${SYSCOUNT_NGINX_TIMEOUT:-8}"
  export SYSCOUNT_NGINX_STARTUP_DELAY="${SYSCOUNT_NGINX_STARTUP_DELAY:-1}"
else
  UPROBE_ITER="${USER_UPROBE_ITER:-10}"
  UPROBE_TEST_ITER="${USER_UPROBE_TEST_ITER:-100000}"
  SSL_NGINX_SIZES="${USER_SSL_NGINX_SIZES:-16b,1kb,2kb,4kb,16kb,32kb,64kb,128kb,256kb}"
fi

if [[ "$(uname -m)" == "aarch64" && "$RUN_SSL_NGINX" == "1" && "$SSL_NGINX_SSLSNIFF_ARGS_IS_SET" == "0" ]]; then
  USER_SSL_NGINX_SSLSNIFF_ARGS="--no-gnutls --no-nss -c nginx"
fi
SSL_NGINX_SSLSNIFF_ARGS="$USER_SSL_NGINX_SSLSNIFF_ARGS"
export SSL_NGINX_SSLSNIFF_ARGS
export SSL_NGINX_STRICT_TRACE_ERRORS

if [[ "$(uname -m)" == "aarch64" && "$RUN_SYSCALL" == "1" && "$ONLY_BENCH" != "syscall" ]]; then
  RUN_SYSCALL=0
  AUTO_SKIPPED_ARM64_SYSCALL=1
fi

log() {
  echo "[$(date '+%F %T')] $*" | tee -a "$OUT/run.log"
}

clean_previous_results() {
  if [[ "$KEEP_PREVIOUS_RESULTS" == "1" ]]; then
    return 0
  fi

  local current_out
  current_out="$(realpath -m "$OUT")"

  find "$REPO" -maxdepth 1 -type d -name 'benchmark-results-*' -print0 2>/dev/null |
    while IFS= read -r -d '' dir; do
      if [[ "$(realpath -m "$dir")" != "$current_out" ]]; then
        rm -rf "$dir"
      fi
    done

  rm -rf \
    "$REPO/benchmark/ssl-nginx/trace_logs" \
    "$REPO/benchmark/syscount-nginx/trace_logs" \
    "$REPO/benchmark/uprobe/trace_logs" \
    "$REPO/benchmark/syscall/trace_logs" \
    "$REPO/benchmark/mpk/trace_logs"

  find "$REPO/benchmark" -maxdepth 3 -type f \
    \( -name 'benchmark-output*.json' \
       -o -name 'benchmark_results*.json' \
       -o -name 'results*.md' \
       -o -name 'benchmark-logs*.txt' \
       -o -name 'benchmark_chart*.png' \
       -o -name 'absolute_performance*.png' \
       -o -name 'relative_performance*.png' \
       -o -name 'throughput_comparison*.png' \
       -o -name 'access.log' \) \
    -delete 2>/dev/null || true
}

clean_previous_results
mkdir -p "$OUT"
touch "$RUN_MARKER"
if [[ "$KEEP_PREVIOUS_RESULTS" == "1" ]]; then
  log "Previous benchmark result cleanup: disabled"
else
  log "Previous benchmark result cleanup: enabled; existing .tar.gz archives are preserved"
fi

run_step() {
  local name="$1"
  shift
  log "===== START: $name ====="
  (
    cd "$REPO"
    "$@"
  ) >"$OUT/${name}.stdout.log" 2>"$OUT/${name}.stderr.log"
  local rc=$?
  echo "$rc" >"$OUT/${name}.rc"
  log "===== END: $name rc=$rc ====="
  if [[ "$rc" != "0" ]]; then
    FAILURES=$((FAILURES + 1))
  fi
  return "$rc"
}

cleanup_bpftime() {
  if [[ -x "$REPO/build/tools/bpftimetool/bpftimetool" ]]; then
    sudo "$REPO/build/tools/bpftimetool/bpftimetool" remove >>"$OUT/run.log" 2>&1 || true
  fi
}

collect_outputs() {
  log "Collecting benchmark output files"
  (
    cd "$REPO"
    if [[ "$RUN_UPROBE" == "1" && -d benchmark/uprobe ]]; then
      find benchmark/uprobe -maxdepth 4 -type f \
        \( -name "*.json" -o -name "*.md" -o -name "*.txt" -o -name "*.png" -o -name "*.log" \) \
        -newer "$RUN_MARKER" \
        -print
    fi
    if [[ "$RUN_SYSCALL" == "1" && -d benchmark/syscall ]]; then
      find benchmark/syscall -maxdepth 4 -type f \
        \( -name "*.json" -o -name "*.md" -o -name "*.txt" -o -name "*.png" -o -name "*.log" \) \
        -newer "$RUN_MARKER" \
        -print
    fi
    if [[ "$RUN_SYSCOUNT" == "1" && -d benchmark/syscount-nginx ]]; then
      find benchmark/syscount-nginx -maxdepth 4 -type f \
        \( -name "*.json" -o -name "*.md" -o -name "*.txt" -o -name "*.png" -o -name "*.log" \) \
        -newer "$RUN_MARKER" \
        ! -name "access.log" \
        ! -path "*/trace_logs/*" \
        -print
    fi
    if [[ "$RUN_SSL_NGINX" == "1" && -d benchmark/ssl-nginx ]]; then
      find benchmark/ssl-nginx -maxdepth 4 -type f \
        \( -name "*.json" -o -name "*.md" -o -name "*.txt" -o -name "*.png" -o -name "*.log" \) \
        -newer "$RUN_MARKER" \
        ! -name "access.log" \
        ! -path "*/trace_logs/*.stdout.log" \
        -print
    fi
    if [[ "$RUN_MPK" == "1" && -d benchmark/mpk ]]; then
      find benchmark/mpk -maxdepth 4 -type f \
        \( -name "*.json" -o -name "*.md" -o -name "*.txt" -o -name "*.png" -o -name "*.log" \) \
        -newer "$RUN_MARKER" \
        -print
    fi
  ) >"$OUT/benchmark-files.txt" 2>/dev/null || true

  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    mkdir -p "$OUT/files/$(dirname "$f")"
    cp -a "$REPO/$f" "$OUT/files/$f" 2>/dev/null || true
  done < "$OUT/benchmark-files.txt"
}

prepare_compat_paths() {
  # benchmark/syscount-nginx/benchmark.py expects this historical path.
  # Some bpftime trees build syscount under example/tracing/syscount instead.
  if [[ ! -e "$REPO/example/libbpf-tools/syscount/syscount" && -e "$REPO/example/tracing/syscount/syscount" ]]; then
    mkdir -p "$REPO/example/libbpf-tools/syscount"
    ln -sf ../../tracing/syscount/syscount "$REPO/example/libbpf-tools/syscount/syscount"
    log "Created compatibility symlink for syscount"
  fi
}

APT_DEPENDENCIES=(
  autoconf
  automake
  binutils-dev
  build-essential
  ca-certificates
  clang
  cmake
  curl
  git
  libboost-all-dev
  libcrypt-dev
  libelf-dev
  libfuse-dev
  libncurses-dev
  libpcre2-dev
  libssl-dev
  libtool
  libyaml-cpp-dev
  llvm
  llvm-dev
  nginx
  pkg-config
  python3
  python3-matplotlib
  python3-numpy
  python3-pip
  systemtap-sdt-dev
  wrk
  zlib1g-dev
)

REQUIRED_TOOLS=(
  cmake
  make
  ninja
  clang
  gcc
  g++
  python3
  sudo
  git
  curl
  llvm-objcopy
  nginx
  wrk
)

missing_required_tools() {
  local cmd
  for cmd in "${REQUIRED_TOOLS[@]}"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      echo "$cmd"
    fi
  done
}

install_native_deps() {
  if ! command -v apt-get >/dev/null 2>&1; then
    log "ERROR: --install-deps requires apt-get; install dependencies manually on this system"
    return 1
  fi

  log "Installing native ARM64 benchmark dependencies with apt-get"
  sudo apt-get update >>"$OUT/run.log" 2>&1
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "${APT_DEPENDENCIES[@]}" >>"$OUT/run.log" 2>&1
}

check_native_deps() {
  local missing
  missing="$(missing_required_tools | paste -sd ' ' -)"
  if [[ -n "$missing" ]]; then
    log "WARNING: missing required tools: $missing"
    log "Install dependencies with: $0 --install-deps [other options]"
    return 1
  fi
  log "Required benchmark tools are available"
  return 0
}

prepare_third_party_deps() {
  if [[ -e "$REPO/third_party/libbpf/src/Makefile" ]]; then
    return 0
  fi

  log "third_party/libbpf is missing; preparing bpftool/libbpf dependency"
  mkdir -p "$REPO/third_party"

  if [[ -d "$REPO/third_party/bpftool/.git" ]]; then
    (
      cd "$REPO/third_party/bpftool"
      git fetch --depth 1 origin master || true
      git submodule update --init --recursive --depth 1
    ) >>"$OUT/run.log" 2>&1 || true
  else
    rm -rf "$REPO/third_party/bpftool"
    git clone --depth 1 --recurse-submodules --shallow-submodules \
      https://github.com/libbpf/bpftool "$REPO/third_party/bpftool" >>"$OUT/run.log" 2>&1 || true
  fi

  if [[ ! -e "$REPO/third_party/bpftool/src/Makefile" ]] && command -v curl >/dev/null 2>&1; then
    log "git clone did not prepare bpftool; trying bpftool tarball fallback"
    rm -rf "$REPO/third_party/bpftool"
    mkdir -p "$REPO/third_party/bpftool"
    curl -L --retry 3 --retry-delay 2 \
      https://github.com/libbpf/bpftool/archive/refs/heads/master.tar.gz \
      -o "$OUT/bpftool-master.tar.gz" >>"$OUT/run.log" 2>&1 || true
    if [[ -s "$OUT/bpftool-master.tar.gz" ]]; then
      tar -xzf "$OUT/bpftool-master.tar.gz" \
        -C "$REPO/third_party/bpftool" --strip-components=1 >>"$OUT/run.log" 2>&1 || true
    fi
  fi

  if [[ ! -e "$REPO/third_party/libbpf/src/Makefile" ]] && command -v curl >/dev/null 2>&1; then
    log "preparing libbpf tarball fallback"
    mkdir -p "$REPO/third_party/bpftool/libbpf"
    curl -L --retry 3 --retry-delay 2 \
      https://github.com/libbpf/libbpf/archive/refs/heads/master.tar.gz \
      -o "$OUT/libbpf-master.tar.gz" >>"$OUT/run.log" 2>&1 || true
    if [[ -s "$OUT/libbpf-master.tar.gz" ]]; then
      tar -xzf "$OUT/libbpf-master.tar.gz" \
        -C "$REPO/third_party/bpftool/libbpf" --strip-components=1 >>"$OUT/run.log" 2>&1 || true
    fi
  fi

  if [[ ! -e "$REPO/third_party/bpftool/src/Makefile" || ! -e "$REPO/third_party/libbpf/src/Makefile" ]]; then
    log "ERROR: failed to prepare third_party/bpftool and third_party/libbpf. Check network access and dependency logs."
    return 1
  fi
}

detect_llvm_dir() {
  local llvm_config_dir

  if [[ -n "$USER_LLVM_DIR" ]]; then
    if [[ -d "$USER_LLVM_DIR" ]]; then
      echo "$USER_LLVM_DIR"
      return 0
    fi
    echo "WARNING: LLVM_DIR/--llvm-dir was set but does not exist: $USER_LLVM_DIR" | tee -a "$OUT/run.log" >&2
  fi

  if command -v llvm-config >/dev/null 2>&1; then
    llvm_config_dir="$(llvm-config --cmakedir 2>/dev/null || true)"
    if [[ -n "$llvm_config_dir" && -d "$llvm_config_dir" ]]; then
      echo "$llvm_config_dir"
      return 0
    fi
  fi

  for ver in 18 17 16 15 14 13 12 11 10 21 20 19; do
    if command -v "llvm-config-$ver" >/dev/null 2>&1; then
      llvm_config_dir="$("llvm-config-$ver" --cmakedir 2>/dev/null || true)"
      if [[ -n "$llvm_config_dir" && -d "$llvm_config_dir" ]]; then
        echo "$llvm_config_dir"
        return 0
      fi
    fi
    if [[ -d "/usr/lib/llvm-$ver/cmake" ]]; then
      echo "/usr/lib/llvm-$ver/cmake"
      return 0
    fi
  done

  return 1
}

log "bpftime ARM64 benchmark runner"
log "Repository: $REPO"
log "Output: $OUT"
log "Mode: $MODE"
log "Run build: $RUN_BUILD"
log "Build only: $BUILD_ONLY"
if [[ "$AUTO_SKIPPED_ARM64_SYSCALL" == "1" ]]; then
  log "Skipping syscall benchmark on AArch64 because the userspace syscall trampoline is not implemented"
fi
log "Selected benchmarks: uprobe=$RUN_UPROBE syscall=$RUN_SYSCALL syscount-nginx=$RUN_SYSCOUNT ssl-nginx=$RUN_SSL_NGINX mpk=$RUN_MPK"
log "UPROBE_ITER: $UPROBE_ITER"
log "UPROBE_TEST_ITER: $UPROBE_TEST_ITER"
log "SSL_NGINX_SIZES: $SSL_NGINX_SIZES"
log "SSL_NGINX_SSLSNIFF_ARGS: ${SSL_NGINX_SSLSNIFF_ARGS:-<default sslsniff args>}"
log "SSL_NGINX_STRICT_TRACE_ERRORS: $SSL_NGINX_STRICT_TRACE_ERRORS"
log "SYSCOUNT_NGINX_BIN: ${SYSCOUNT_NGINX_BIN:-nginx}"
log "MPK_ITER: $MPK_ITER"

if [[ ! -d "$REPO" ]]; then
  log "ERROR: repository path does not exist: $REPO"
  exit 1
fi

if [[ ! -f "$REPO/Makefile" || ! -d "$REPO/benchmark" ]]; then
  log "ERROR: this does not look like the bpftime repository: $REPO"
  exit 1
fi

log "System information"
{
  echo "date: $(date -Is)"
  echo "uname: $(uname -a)"
  echo "arch: $(uname -m)"
  echo "nproc: $(nproc)"
  echo
  cat /etc/os-release 2>/dev/null || true
} | tee "$OUT/system-info.txt" | tee -a "$OUT/run.log" >/dev/null

log "Git information"
(
  cd "$REPO"
  git rev-parse HEAD 2>/dev/null || true
  git status --short 2>/dev/null || true
) | tee "$OUT/git-info.txt" | tee -a "$OUT/run.log" >/dev/null

if [[ "$INSTALL_DEPS" == "1" ]]; then
  if ! install_native_deps; then
    log "Dependency installation failed; continuing so logs can be collected"
    FAILURES=$((FAILURES + 1))
  fi
fi

if [[ "$CHECK_DEPS" == "1" ]]; then
  check_native_deps || true
fi

log "Tool availability"
for cmd in cmake make ninja clang gcc g++ python3 sudo bpftool nginx wrk git curl llvm-objcopy; do
  if command -v "$cmd" >/dev/null 2>&1; then
    echo "$cmd: $(command -v "$cmd")"
  else
    echo "$cmd: MISSING"
  fi
done | tee "$OUT/tools.txt" | tee -a "$OUT/run.log" >/dev/null

if [[ "$RUN_BUILD" == "1" ]]; then
  if ! prepare_third_party_deps; then
    log "Build cannot continue without third_party/libbpf"
    FAILURES=$((FAILURES + 1))
    RUN_UPROBE=0
    RUN_SYSCALL=0
    RUN_SYSCOUNT=0
    RUN_SSL_NGINX=0
    RUN_MPK=0
  fi

  LLVM_CMAKE_ARG=""
  if LLVM_CMAKE_DIR="$(detect_llvm_dir)"; then
    log "Using LLVM CMake directory: $LLVM_CMAKE_DIR"
    LLVM_CMAKE_ARG="-DLLVM_DIR=$LLVM_CMAKE_DIR"
  else
    log "WARNING: LLVM CMake directory was not detected. CMake will use its default search paths."
  fi
  export LLVM_CMAKE_ARG

  if [[ "$FAILURES" == "0" ]]; then
    BUILD_ATTACH_IMPL_EXAMPLE_FLAG=0
    if [[ "$RUN_SYSCOUNT" == "1" || "$RUN_SSL_NGINX" == "1" ]]; then
      BUILD_ATTACH_IMPL_EXAMPLE_FLAG=1
    fi
    export BUILD_ATTACH_IMPL_EXAMPLE_FLAG
    export RUN_UPROBE RUN_SYSCALL RUN_SYSCOUNT RUN_SSL_NGINX RUN_MPK

    if ! run_step build_bpftime_non_mpk bash -lc '
    set -e
    cmake -Bbuild ${LLVM_CMAKE_ARG} \
      -DCMAKE_BUILD_TYPE:STRING=RelWithDebInfo \
      -DBPFTIME_LLVM_JIT=1 \
      -DBPFTIME_ENABLE_LTO=1 \
      -DSPDLOG_ACTIVE_LEVEL=SPDLOG_LEVEL_INFO \
      -DENABLE_PROBE_WRITE_CHECK=0 \
      -DENABLE_PROBE_READ_CHECK=0 \
      -DBPFTIME_ENABLE_MPK=0 \
      -DBUILD_ATTACH_IMPL_EXAMPLE=${BUILD_ATTACH_IMPL_EXAMPLE_FLAG}
    cmake --build build --config RelWithDebInfo --target install -j"$(nproc)"
    if [[ "$BUILD_ATTACH_IMPL_EXAMPLE_FLAG" == "1" ]]; then
      cmake --build build --config RelWithDebInfo --target attach_impl_example_nginx -j"$(nproc)" || true
    fi
    if [[ "$RUN_UPROBE" == "1" || "$RUN_MPK" == "1" ]]; then
      make -C benchmark test
    fi
    if [[ "$RUN_UPROBE" == "1" ]]; then
      make -C benchmark/uprobe
    fi
    if [[ "$RUN_SYSCALL" == "1" ]]; then
      make -C benchmark/syscall
    fi
    if [[ "$RUN_SYSCOUNT" == "1" ]]; then
      make -C benchmark/syscount-nginx
    fi
    if [[ "$RUN_SSL_NGINX" == "1" ]]; then
      make -C benchmark/ssl-nginx
    fi
    '; then
      log "Build failed; skipping benchmark execution"
      RUN_UPROBE=0
      RUN_SYSCALL=0
      RUN_SYSCOUNT=0
      RUN_SSL_NGINX=0
      RUN_MPK=0
    fi

    if [[ "$RUN_MPK" == "1" ]]; then
      if ! run_step build_bpftime_mpk bash -lc '
      set -e
      cmake -Bbuild-mpk ${LLVM_CMAKE_ARG} \
        -DCMAKE_BUILD_TYPE:STRING=RelWithDebInfo \
        -DBPFTIME_LLVM_JIT=1 \
        -DBPFTIME_ENABLE_LTO=1 \
        -DSPDLOG_ACTIVE_LEVEL=SPDLOG_LEVEL_INFO \
        -DENABLE_PROBE_WRITE_CHECK=0 \
        -DENABLE_PROBE_READ_CHECK=0 \
        -DBPFTIME_ENABLE_MPK=1 \
        -DBUILD_ATTACH_IMPL_EXAMPLE=0
      cmake --build build-mpk --config RelWithDebInfo --target install -j"$(nproc)"
      '; then
        log "MPK build failed; skipping MPK benchmark execution"
        RUN_MPK=0
      fi
    fi
  fi
else
  log "Skipping build because RUN_BUILD=0"
fi

prepare_compat_paths
cleanup_bpftime

if [[ "$BUILD_ONLY" == "1" ]]; then
  log "Build-only mode; skipping benchmark execution"
  RUN_UPROBE=0
  RUN_SYSCALL=0
  RUN_SYSCOUNT=0
  RUN_SSL_NGINX=0
  RUN_MPK=0
fi

if [[ "$RUN_UPROBE" == "1" ]]; then
  if [[ ! -e /sys/kernel/btf/vmlinux ]]; then
    log "Kernel BTF is missing at /sys/kernel/btf/vmlinux; benchmark/uprobe strips .BTF.ext so kernel uprobe can still run"
  fi
  run_step uprobe python3 benchmark/uprobe/benchmark.py --iter "$UPROBE_ITER" --test-iter "$UPROBE_TEST_ITER"
  cleanup_bpftime
fi

if [[ "$RUN_SYSCALL" == "1" ]]; then
  run_step syscall python3 benchmark/syscall/benchmark.py
  cleanup_bpftime
fi

if [[ "$RUN_SYSCOUNT" == "1" ]]; then
  run_step syscount_nginx python3 benchmark/syscount-nginx/benchmark.py
  cleanup_bpftime
fi

if [[ "$RUN_SSL_NGINX" == "1" ]]; then
  run_step ssl_nginx env SSL_NGINX_SIZES="$SSL_NGINX_SIZES" SSL_NGINX_SSLSNIFF_ARGS="$SSL_NGINX_SSLSNIFF_ARGS" SSL_NGINX_STRICT_TRACE_ERRORS="$SSL_NGINX_STRICT_TRACE_ERRORS" python3 benchmark/ssl-nginx/draw_figture.py
  cleanup_bpftime
fi

if [[ "$RUN_MPK" == "1" ]]; then
  run_step mpk python3 benchmark/mpk/benchmark.py --iter "$MPK_ITER"
  cleanup_bpftime
else
  log "Skipping MPK benchmark"
fi

collect_outputs

log "Creating archive"
tar_log="$(mktemp)"
if tar -czf "$OUT.tar.gz" -C "$(dirname "$OUT")" "$(basename "$OUT")" >"$tar_log" 2>&1; then
  cat "$tar_log" >>"$OUT/run.log"
else
  cat "$tar_log" >>"$OUT/run.log"
  log "WARNING: failed to create archive"
fi
rm -f "$tar_log"

log "Done"
log "Result directory: $OUT"
log "Archive: $OUT.tar.gz"
log "Please send back the .tar.gz archive and run.log if possible."

if [[ "$FAILURES" != "0" ]]; then
  log "Completed with $FAILURES failed step(s)"
  exit 1
fi
