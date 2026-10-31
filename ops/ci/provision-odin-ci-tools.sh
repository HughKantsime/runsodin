#!/usr/bin/env bash
# Second provisioning pass for odin-ci: the gate's security tools at the macOS runner's
# versions, and QEMU binfmt for the registry contract in CV01, which builds a small
# linux/amd64,linux/arm64 image. Publishing doesn't run here. gitleaks and hadolint are
# verified against committed hashes and the published checksum files. semgrep is
# version-pinned only: its dependencies are unhashed.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=ops/ci/runner-lib.sh
. "$here/runner-lib.sh"
GITLEAKS=8.30.1
HADOLINT=2.14.0
SEMGREP=1.163.0
# Expected hashes are committed here (verified 2026-10-30), so a replaced release asset
# and checksum file together cannot pass. The published checksum files must agree too.
GITLEAKS_SHA256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
HADOLINT_SHA256=6bf226944684f56c84dd014e8b979d27425c0148f61b3bd99bcc6f39e9dc5a47
export DEBIAN_FRONTEND=noninteractive
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT; cd "$tmp"

sudo apt-get install -yq qemu-user-static binfmt-support
sudo systemctl restart systemd-binfmt || true

curl -fsSLO "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS}/gitleaks_${GITLEAKS}_linux_x64.tar.gz"
curl -fsSLO "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS}/gitleaks_${GITLEAKS}_checksums.txt"
grep " gitleaks_${GITLEAKS}_linux_x64.tar.gz\$" "gitleaks_${GITLEAKS}_checksums.txt" | sha256sum -c -
echo "${GITLEAKS_SHA256}  gitleaks_${GITLEAKS}_linux_x64.tar.gz" | sha256sum -c -
tar -xzf "gitleaks_${GITLEAKS}_linux_x64.tar.gz" gitleaks && sudo install -m 0755 gitleaks /usr/local/bin/gitleaks

curl -fsSLO "https://github.com/hadolint/hadolint/releases/download/v${HADOLINT}/hadolint-linux-x86_64"
curl -fsSLO "https://github.com/hadolint/hadolint/releases/download/v${HADOLINT}/hadolint-linux-x86_64.sha256"
echo "$(awk '{print $1}' hadolint-linux-x86_64.sha256)  hadolint-linux-x86_64" | sha256sum -c -
echo "${HADOLINT_SHA256}  hadolint-linux-x86_64" | sha256sum -c -
sudo install -m 0755 hadolint-linux-x86_64 /usr/local/bin/hadolint

# semgrep in its own venv; only the entry point goes on PATH.
sudo python3.11 -m venv /opt/semgrep
sudo /opt/semgrep/bin/pip install -q --disable-pip-version-check "semgrep==${SEMGREP}"
sudo ln -sf /opt/semgrep/bin/semgrep /usr/local/bin/semgrep

# Gates refuse protected files under a world-writable parent, so jobs can't use /tmp.
# Keep it outside $HOME: pytest roots a run at the folder shared by the checkout and the
# test path, and test ids that assume no shared folder would change.
ci_tmp=/opt/odin-ci-tmp
sudo install -d -m 700 -o "$USER" -g "$USER" "$ci_tmp"
echo "d $ci_tmp 0700 $USER $USER 7d" | sudo tee /etc/tmpfiles.d/odin-ci.conf >/dev/null
if runner_env_set TMPDIR "$ci_tmp"; then
  runner_restart
fi

# The vendor profile tests skip without Bambu Studio's machine profiles, and CV02 refuses
# skips. Copy them from any install; this path is the tests' Linux default:
#   rsync -a /Applications/BambuStudio.app/Contents/Resources/profiles/BBL/machine/ \
#     odin-ci:/usr/share/bambu-studio/profiles/BBL/machine/
test -d /usr/share/bambu-studio/profiles/BBL/machine || echo "WARNING: Bambu Studio profiles missing; CV02 will fail"

# Host side, on pve3 (not run here): zfs set sync=disabled rpool/data/vm-130-disk-0.
# CV02's SQLite fsyncs went from 168 kB/s to 16.9 MB/s; the disk only holds rebuildable CI state.

cd /
echo "versions:"; gitleaks version; hadolint --version; semgrep --version
# Pull the host (amd64) variant first: the gate's capacity probe runs this digest with
# --pull=never and no --platform, and an arm64-only local copy adds a platform warning.
docker pull -q --platform linux/amd64 python:3.11-slim@sha256:0b23cfb7425d065008b778022a17b1551c82f8b4866ee5a7a200084b7e2eafbf >/dev/null
docker run --rm --platform linux/arm64 python:3.11-slim@sha256:0b23cfb7425d065008b778022a17b1551c82f8b4866ee5a7a200084b7e2eafbf python3 -c 'import platform; print("arm64 emulation:", platform.machine())'
