#!/usr/bin/env bash
# setup.sh - Install all dependencies for bpftime-benchmark on Ubuntu 26.04+
# Usage: bash setup.sh

set -e

echo "==> Installing system dependencies..."
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

echo "==> Installing Python dependencies..."
pip install -r benchmark/requirements.txt --break-system-packages

echo ""
echo "All dependencies installed. You can now run:"
echo "  make benchmark"
