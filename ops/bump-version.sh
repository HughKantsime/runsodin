#!/usr/bin/env bash
# bump-version.sh — Update version across all files from VERSION (single source of truth)
# Usage: ./ops/bump-version.sh 1.0.23
#   or:  ./ops/bump-version.sh        (reads current VERSION file)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ $# -ge 1 ]; then
    VERSION="$1"
    echo "$VERSION" > "$REPO_ROOT/VERSION"
else
    VERSION="$(cat "$REPO_ROOT/VERSION" | tr -d '[:space:]')"
fi

echo "Bumping all version references to $VERSION"

# 1. frontend/package.json
node -e "
  const fs = require('fs');
  const path = '$REPO_ROOT/frontend/package.json';
  const pkg = JSON.parse(fs.readFileSync(path, 'utf-8'));
  pkg.version = '$VERSION';
  fs.writeFileSync(path, JSON.stringify(pkg, null, 2) + '\n');
"
echo "  ✓ frontend/package.json → $VERSION"

# 2. backend/main.py fallback version
sed -i "s/__version__ = \"[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\"/__version__ = \"$VERSION\"/" \
    "$REPO_ROOT/backend/main.py"
echo "  ✓ backend/main.py fallback → $VERSION"

echo ""
echo "Done. Dynamic sources (vite.config.js, backend runtime, CI tags) read VERSION automatically."
echo "Next steps:"
echo "  git add -A && git commit -m \"release: v$VERSION\""
echo "  git tag v$VERSION"
echo "  git push origin main v$VERSION"
