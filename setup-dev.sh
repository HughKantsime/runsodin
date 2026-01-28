#!/bin/bash
# PrintFarm Scheduler - Local Development Setup
# Run this script to get the backend running locally without Docker

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

echo "🖨️  PrintFarm Scheduler - Local Dev Setup"
echo "=========================================="

# Check Python version
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is required but not installed."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "✓ Found Python $PYTHON_VERSION"

# Create virtual environment if it doesn't exist
if [ ! -d "$BACKEND_DIR/venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv "$BACKEND_DIR/venv"
fi

# Activate virtual environment
echo "🔧 Activating virtual environment..."
source "$BACKEND_DIR/venv/bin/activate"

# Install dependencies
echo "📥 Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r "$BACKEND_DIR/requirements.txt"

# Create .env if it doesn't exist
if [ ! -f "$BACKEND_DIR/.env" ]; then
    echo "📝 Creating .env file..."
    cat > "$BACKEND_DIR/.env" << EOF
DATABASE_URL=sqlite:///./printfarm.db
DEBUG=true
HOST=0.0.0.0
PORT=8000
# SPOOLMAN_URL=http://localhost:7912
BLACKOUT_START=22:30
BLACKOUT_END=05:30
EOF
fi

echo ""
echo "✅ Setup complete!"
echo ""
echo "To start the server, run:"
echo "  cd backend"
echo "  source venv/bin/activate"
echo "  uvicorn main:app --reload"
echo ""
echo "Then open:"
echo "  API Docs: http://localhost:8000/docs"
echo "  Health:   http://localhost:8000/health"
echo ""
