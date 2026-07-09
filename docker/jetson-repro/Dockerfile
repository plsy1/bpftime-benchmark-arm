FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /bpftime

RUN apt-get update && apt-get install -y --no-install-recommends \
    autoconf \
    automake \
    binutils-dev \
    bpftool \
    build-essential \
    ca-certificates \
    clang \
    cmake \
    curl \
    git \
    libboost-all-dev \
    libcrypt-dev \
    libelf-dev \
    libfuse-dev \
    libncurses-dev \
    libpcre2-dev \
    libssl-dev \
    libtool \
    libyaml-cpp-dev \
    llvm \
    llvm-dev \
    nginx \
    ninja-build \
    pkg-config \
    procps \
    python3 \
    python3-matplotlib \
    python3-numpy \
    python3-pip \
    sudo \
    systemtap-sdt-dev \
    wrk \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY . /bpftime

RUN cmake -Bbuild \
      -DCMAKE_BUILD_TYPE:STRING=RelWithDebInfo \
      -DBPFTIME_LLVM_JIT=1 \
      -DBPFTIME_ENABLE_LTO=1 \
      -DSPDLOG_ACTIVE_LEVEL=SPDLOG_LEVEL_INFO \
      -DENABLE_PROBE_WRITE_CHECK=0 \
      -DENABLE_PROBE_READ_CHECK=0 \
      -DBPFTIME_ENABLE_MPK=0 \
      -DBUILD_ATTACH_IMPL_EXAMPLE=1 \
    && cmake --build build --config RelWithDebInfo --target install -j"$(nproc)" \
    && (cmake --build build --config RelWithDebInfo --target attach_impl_example_nginx -j"$(nproc)" || true)

RUN make -C benchmark test \
    && make -C benchmark/uprobe \
    && make -C benchmark/syscount-nginx \
    && make -C benchmark/ssl-nginx

ENV SSL_NGINX_SSLSNIFF_ARGS="--no-gnutls --no-nss -c nginx"
ENV SYSCOUNT_NGINX_BIN="nginx"

CMD ["./run_bpftime_arm64_benchmarks.sh", "--mode", "smoke", "--skip-syscall"]
