#!/usr/bin/env bash
# ============================================================
# bump-version.sh — Bump version, commit, tag, and optionally push
#
# Usage:
#   ./ops/bump-version.sh 1.0.29            # bump + commit + tag (no push)
#   ./ops/bump-version.sh 1.0.29 --push     # bump + commit + tag + push
#   ./ops/bump-version.sh                    # show current version
#
# This script ensures the version bump commit is created BEFORE
# the git tag, so the Docker image always contains the correct
# VERSION file.
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

DO_PUSH=false

# Parse args
for arg in "$@"; do
    case "$arg" in
        --push) DO_PUSH=true ;;
    esac
done

# Get version argument (first non-flag arg)
VERSION=""
for arg in "$@"; do
    case "$arg" in
        --*) ;; # skip flags
        *) VERSION="$arg"; break ;;
    esac
done

# If no version given, show current and exit
if [ -z "$VERSION" ]; then
    CURRENT="$(cat "$REPO_ROOT/VERSION" | tr -d '[:space:]')"
    echo "Current version: $CURRENT"
    echo ""
    echo "Usage: ./ops/bump-version.sh <new-version> [--push]"
    exit 0
fi

CURRENT="$(cat "$REPO_ROOT/VERSION" | tr -d '[:space:]')"
echo -e "${BOLD}Bumping O.D.I.N. version: ${CURRENT} → ${VERSION}${NC}"

# --- Safety checks ---
step "Pre-flight checks"

cd "$REPO_ROOT"

# Must be on main/master
BRANCH=$(git branch --show-current)
if [[ "$BRANCH" != "main" && "$BRANCH" != "master" ]]; then
    die "Must be on main or master branch (currently on: ${BRANCH})"
fi

# Working tree must be clean
if [[ -n "$(git status --porcelain)" ]]; then
    die "Working tree is dirty — commit or stash changes first"
fi

# Tag must not already exist
if git rev-parse "v${VERSION}" >/dev/null 2>&1; then
    die "Tag v${VERSION} already exists. Delete it first if re-releasing."
fi

ok "On branch ${BRANCH}, clean tree, tag v${VERSION} is available"

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
else
    echo "  ⚠ node not found — skipping frontend/package.json"
fi

# backend/main.py fallback version
sedi "s/__version__ = \"[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\"/__version__ = \"$VERSION\"/" \
    "$REPO_ROOT/backend/main.py"
ok "backend/main.py fallback → $VERSION"

# docker-compose.yml image tag
GHCR_IMAGE="ghcr.io/hughkantsime/odin"
sedi "s|image: ${GHCR_IMAGE}:.*|image: ${GHCR_IMAGE}:v${VERSION}|" \
    "$REPO_ROOT/docker-compose.yml"
ok "docker-compose.yml → v$VERSION"

sedi "s/ODIN_VERSION=\"[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\"/ODIN_VERSION=\"$VERSION\"/" \
    "$REPO_ROOT/install/install.sh"
ok "install/install.sh → $VERSION"

# --- Step 2: Commit ---
step "Creating version bump commit"

git add VERSION frontend/package.json backend/main.py docker-compose.yml install/install.sh
git commit -m "release: bump version to $VERSION"
ok "Committed: release: bump version to $VERSION"

# --- Step 3: Tag (on the bump commit, not before it) ---
step "Creating git tag"

git tag "v${VERSION}"
ok "Tagged: v${VERSION} → $(git rev-parse --short HEAD)"

# --- Step 4: Push (optional) ---
if [[ "$DO_PUSH" == true ]]; then
    step "Pushing to origin"
    git push origin "$BRANCH" "v${VERSION}"
    ok "Pushed branch ${BRANCH} and tag v${VERSION}"
else
    echo ""
    echo -e "${BOLD}Ready to push. Run:${NC}"
    echo "  git push origin ${BRANCH} v${VERSION}"
fi

echo ""
echo -e "${GREEN}${BOLD}✅ Version $VERSION is ready${NC}"
echo ""
echo "  VERSION file:     $VERSION"
echo "  Git tag:          v${VERSION} → $(git rev-parse --short HEAD)"
echo "  Docker workflow:  will trigger on push of tag v${VERSION}"
echo ""
