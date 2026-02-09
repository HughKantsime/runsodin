#!/bin/bash
# O.D.I.N. Update Script
# Usage: ./update.sh
set -e

cd "$(dirname "$0")"

echo "🔄 Updating O.D.I.N...."

# Pull latest code
git pull origin master

# Rebuild and restart (zero downtime isn't possible with single container)
docker compose build --quiet
docker compose up -d

echo ""
echo "✅ Updated to $(git describe --tags --always)"
echo "   Your data in ./odin-data/ is untouched."
