#!/usr/bin/env bash
# Second provisioning pass for odin-ci: the gate's security tools at the macOS
# runner's versions, QEMU binfmt for the arm64 image half, and jsonschema for the
# host python3.11 used by the production-mutation workflows. Release binaries are
# verified against the project's published checksum files.
set -euo pipefail
GITLEAKS=8.30.1
HADOLINT=2.14.0
GH=2.89.0
SEMGREP=1.163.0
JSONSCHEMA=4.25.1
export DEBIAN_FRONTEND=noninteractive
tmp=$(mktemp -d); cd "$tmp"

sudo apt-get install -yq qemu-user-static binfmt-support
sudo systemctl restart systemd-binfmt || true

curl -fsSLO "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS}/gitleaks_${GITLEAKS}_linux_x64.tar.gz"
curl -fsSLO "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS}/gitleaks_${GITLEAKS}_checksums.txt"
grep " gitleaks_${GITLEAKS}_linux_x64.tar.gz\$" "gitleaks_${GITLEAKS}_checksums.txt" | sha256sum -c -
tar -xzf "gitleaks_${GITLEAKS}_linux_x64.tar.gz" gitleaks && sudo install -m 0755 gitleaks /usr/local/bin/gitleaks

curl -fsSLO "https://github.com/hadolint/hadolint/releases/download/v${HADOLINT}/hadolint-linux-x86_64"
curl -fsSLO "https://github.com/hadolint/hadolint/releases/download/v${HADOLINT}/hadolint-linux-x86_64.sha256"
echo "$(awk '{print $1}' hadolint-linux-x86_64.sha256)  hadolint-linux-x86_64" | sha256sum -c -
sudo install -m 0755 hadolint-linux-x86_64 /usr/local/bin/hadolint

curl -fsSLO "https://github.com/cli/cli/releases/download/v${GH}/gh_${GH}_linux_amd64.deb"
curl -fsSLO "https://github.com/cli/cli/releases/download/v${GH}/gh_${GH}_checksums.txt"
grep " gh_${GH}_linux_amd64.deb\$" "gh_${GH}_checksums.txt" | sha256sum -c -
sudo apt-get install -yq "./gh_${GH}_linux_amd64.deb"

# semgrep in its own venv; only the entry point goes on PATH.
sudo python3.11 -m venv /opt/semgrep
sudo /opt/semgrep/bin/pip install -q --disable-pip-version-check "semgrep==${SEMGREP}"
sudo ln -sf /opt/semgrep/bin/semgrep /usr/local/bin/semgrep

# jsonschema for the host interpreter, user site of the runner account.
python3.11 -m ensurepip --user >/dev/null 2>&1 || true
python3.11 -m pip install -q --user --break-system-packages --disable-pip-version-check "jsonschema==${JSONSCHEMA}"

cd /; rm -rf "$tmp"
echo "versions:"; gitleaks version; hadolint --version; gh --version | head -1; semgrep --version
python3.11 -c 'import jsonschema,importlib.metadata as m; print("jsonschema", m.version("jsonschema"))'
docker run --rm --platform linux/arm64 python:3.11-slim@sha256:0b23cfb7425d065008b778022a17b1551c82f8b4866ee5a7a200084b7e2eafbf python3 -c 'import platform; print("arm64 emulation:", platform.machine())'
