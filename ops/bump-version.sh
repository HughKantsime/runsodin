#!/usr/bin/env bash
# ============================================================
# bump-version.sh — Bump version files and create one local commit
#
# Usage:
#   ./ops/bump-version.sh 1.9.13            # bump + local commit only
#   ./ops/bump-version.sh                    # show current version
#
# Publication, tagging, pushing, and deployment are intentionally owned by the
# reviewed promotion workflow, not by a developer-side helper.
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Cross-platform sed -i wrapper (GNU vs BSD)
if [[ "$(uname)" == "Darwin" ]]; then
    sedi() { sed -i '' "$@"; }
else
    sedi() { sed -i "$@"; }
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $1"; }
die()  { echo -e "${RED}✗ $1${NC}"; exit 1; }
step() { echo -e "\n${CYAN}${BOLD}▶ $1${NC}"; }

if [ "$#" -gt 1 ]; then
    [ "${2:-}" = "--push" ] && die "--push is disabled; use the reviewed promotion workflow"
    die "Unknown argument: ${2:-}"
fi
if [ "${1:-}" = "--push" ]; then
    die "--push is disabled; use the reviewed promotion workflow"
fi
case "${1:-}" in
    --*) die "Unknown argument: $1" ;;
esac
VERSION="${1:-}"

# If no version given, show current and exit
if [ -z "$VERSION" ]; then
    CURRENT="$(cat "$REPO_ROOT/VERSION" | tr -d '[:space:]')"
    echo "Current version: $CURRENT"
    echo ""
    echo "Usage: ./ops/bump-version.sh <new-version>"
    exit 0
fi
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Version must use X.Y.Z numeric format"

CURRENT="$(cat "$REPO_ROOT/VERSION" | tr -d '[:space:]')"
echo -e "${BOLD}Bumping O.D.I.N. version: ${CURRENT} → ${VERSION}${NC}"

# --- Safety checks ---
step "Pre-flight checks"

cd "$REPO_ROOT"

# Must be on main
BRANCH=$(git branch --show-current)
if [[ "$BRANCH" != "main" ]]; then
    die "Must be on main branch (currently on: ${BRANCH})"
fi

# Working tree must be clean
if [[ -n "$(git status --porcelain)" ]]; then
    die "Working tree is dirty — commit or stash changes first"
fi

ok "On branch ${BRANCH} with a clean tree"

# --- Step 1: Update version files ---
step "Updating version files"

echo "$VERSION" > "$REPO_ROOT/VERSION"
ok "VERSION → $VERSION"

# frontend/package.json
if command -v node &>/dev/null; then
    node -e "
      const fs = require('fs');
      const path = '$REPO_ROOT/frontend/package.json';
      const pkg = JSON.parse(fs.readFileSync(path, 'utf-8'));
      pkg.version = '$VERSION';
      fs.writeFileSync(path, JSON.stringify(pkg, null, 2) + '\n');
    "
    ok "frontend/package.json → $VERSION"
    node -e "
      const fs = require('fs');
      const path = '$REPO_ROOT/frontend/package-lock.json';
      const lock = JSON.parse(fs.readFileSync(path, 'utf-8'));
      lock.version = '$VERSION';
      if (lock.packages && lock.packages['']) {
        lock.packages[''].version = '$VERSION';
      }
      fs.writeFileSync(path, JSON.stringify(lock, null, 2) + '\n');
    "
    ok "frontend/package-lock.json → $VERSION"
else
    echo "  ⚠ node not found — skipping frontend/package.json and package-lock.json"
fi

# backend/core/app.py fallback version
sedi "s/__version__ = \"[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\"/__version__ = \"$VERSION\"/" \
    "$REPO_ROOT/backend/core/app.py"
ok "backend/core/app.py fallback → $VERSION"

# docker-compose.yml image tag
GHCR_IMAGE="ghcr.io/hughkantsime/odin"
sedi "s|image: ${GHCR_IMAGE}:.*|image: ${GHCR_IMAGE}:v${VERSION}|" \
    "$REPO_ROOT/docker-compose.yml"
ok "docker-compose.yml → v$VERSION"

sedi "s/ODIN_VERSION=\"[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\"/ODIN_VERSION=\"$VERSION\"/" \
    "$REPO_ROOT/install/install.sh"
ok "install/install.sh → $VERSION"

sedi "s/\$ODIN_VERSION = \"[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\"/\$ODIN_VERSION = \"$VERSION\"/" \
    "$REPO_ROOT/install/install.ps1"
ok "install/install.ps1 → $VERSION"

# frontend/public/sw.js cache version
sedi "s/const CACHE_NAME = 'odin-v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*'/const CACHE_NAME = 'odin-v$VERSION'/" \
    "$REPO_ROOT/frontend/public/sw.js"
ok "frontend/public/sw.js → odin-v$VERSION"

# --- Step 1b: Regenerate design tokens ---
step "Regenerating design tokens"

if command -v node &>/dev/null && [ -f "$REPO_ROOT/design/generate.mjs" ]; then
    node "$REPO_ROOT/design/generate.mjs" --local-only
    ok "Design tokens regenerated"
else
    die "node or design/generate.mjs not found — cannot regenerate tokens"
fi

# --- Step 2: Commit ---
step "Creating version bump commit"

git add VERSION frontend/package.json frontend/package-lock.json backend/core/app.py docker-compose.yml install/install.sh install/install.ps1 frontend/public/sw.js frontend/src/design-tokens.css design/
git commit -m "release: bump version to $VERSION"
ok "Committed: release: bump version to $VERSION"

echo ""
echo -e "${GREEN}${BOLD}✅ Version $VERSION committed locally${NC}"
echo ""
echo "  VERSION file:     $VERSION"
echo "  Commit:           $(git rev-parse --short HEAD)"
echo "  Next step:        reviewed promotion workflow"
echo ""
