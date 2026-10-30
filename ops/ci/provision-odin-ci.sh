#!/usr/bin/env bash
# Provision the odin-ci GitHub Actions runner host (Ubuntu 24.04, amd64).
# Toolchain matches the macOS runner it replaces: Python 3.11, Node 25.9.0,
# mosquitto, Docker Engine, git-lfs, Playwright Chromium system deps.
# Every download is from the project's official source; tarballs are SHA256-verified.
set -euo pipefail
NODE_VERSION=25.9.0
NODE_SHA256=1d8db7d6e291d167e8c467ae4094be175e1a0b3969c7ae1f8955b9f7824f7b2e
RUNNER_VERSION=2.338.2
RUNNER_SHA256=baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40
PLAYWRIGHT=1.62.0   # matches tests/requirements-contracts.txt
# Docker Engine 29.x is what the release workflows accept; pin and hold it.
DOCKER_PKGS="docker-ce=5:29.9.2-1~ubuntu.24.04~noble docker-ce-cli=5:29.9.2-1~ubuntu.24.04~noble containerd.io=2.4.2-3~ubuntu.24.04~noble docker-buildx-plugin=0.38.1-1~ubuntu.24.04~noble docker-compose-plugin=5.6.1-1~ubuntu.24.04~noble"
export DEBIAN_FRONTEND=noninteractive

sudo apt-get update -q
sudo apt-get install -yq ca-certificates curl gnupg software-properties-common git git-lfs make \
  build-essential jq unzip xz-utils mosquitto qemu-guest-agent sqlite3 libpq-dev pkg-config
sudo systemctl enable --now qemu-guest-agent
# The validation suite starts its own broker; the packaged service must not hold :1883.
sudo systemctl disable --now mosquitto

# Python 3.11 (Ubuntu 24.04 ships 3.12) from the deadsnakes PPA.
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update -q
sudo apt-get install -yq python3.11 python3.11-venv python3.11-dev

# Docker Engine from Docker's apt repository (native amd64 builds; no emulation).
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu noble stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update -q
sudo apt-get install -yq --allow-downgrades $DOCKER_PKGS
sudo apt-mark hold docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker odin

# Node from nodejs.org, pinned and verified.
cd /tmp
curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz"
echo "${NODE_SHA256}  node-v${NODE_VERSION}-linux-x64.tar.xz" | sha256sum -c -
sudo tar -xJf "node-v${NODE_VERSION}-linux-x64.tar.xz" -C /usr/local --strip-components=1
rm "node-v${NODE_VERSION}-linux-x64.tar.xz"

# Playwright Chromium system libraries (browser binaries are installed per run by the workflow).
pw=$(mktemp -d); trap 'rm -rf "$pw"' EXIT
python3.11 -m venv "$pw/venv" && "$pw/venv/bin/pip" install -q --disable-pip-version-check "playwright==${PLAYWRIGHT}"
sudo "$pw/venv/bin/playwright" install-deps chromium

# GitHub Actions runner, unregistered (registration is a separate step).
mkdir -p "$HOME/actions-runner-runsodin" && cd "$HOME/actions-runner-runsodin"
curl -fsSLo runner.tar.gz "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
echo "${RUNNER_SHA256}  runner.tar.gz" | sha256sum -c -
tar -xzf runner.tar.gz && rm runner.tar.gz
sudo ./bin/installdependencies.sh

git lfs install --skip-repo
echo "versions:"; python3.11 --version; node --version; npm --version; docker --version; { mosquitto -h 2>&1 || true; } | head -1; git lfs version  # -h exits 3
