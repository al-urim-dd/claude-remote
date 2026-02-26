#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="$HOME/.claude-remote"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== ClaudeRemote Setup ==="
echo ""

# 1. Create config directory
echo "Creating config directory at $CONFIG_DIR ..."
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

# 2. Create venv and install Python dependencies
VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment at $VENV_DIR ..."
    if command -v uv &>/dev/null; then
        uv venv "$VENV_DIR"
    else
        python3 -m venv "$VENV_DIR"
    fi
fi

echo "Installing Python dependencies..."
if command -v uv &>/dev/null; then
    uv pip install --python "$VENV_DIR/bin/python" -r "$SCRIPT_DIR/requirements.txt"
else
    "$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"
fi

# 3. Check for client_secret.json
if [ ! -f "$CONFIG_DIR/client_secret.json" ]; then
    echo ""
    echo "=== ACTION REQUIRED ==="
    echo "Place your OAuth client_secret.json at:"
    echo "  $CONFIG_DIR/client_secret.json"
    echo ""
    echo "To get this file:"
    echo "  1. Go to console.cloud.google.com"
    echo "  2. Create/select a project, enable Gmail API"
    echo "  3. Create OAuth 2.0 Client ID (Desktop type)"
    echo "  4. Download the JSON and save it as client_secret.json"
    echo ""
    read -p "Press Enter once you've placed client_secret.json... "
fi

if [ ! -f "$CONFIG_DIR/client_secret.json" ]; then
    echo "ERROR: client_secret.json not found at $CONFIG_DIR/client_secret.json"
    exit 1
fi

chmod 600 "$CONFIG_DIR/client_secret.json"

# 4. Run initial OAuth flow
echo ""
echo "Running initial OAuth flow (this will open your browser)..."
"$VENV_DIR/bin/python" -c "
import os, sys
sys.path.insert(0, '$SCRIPT_DIR')
from bridge import authenticate
authenticate()
print('OAuth setup complete! Token cached at $CONFIG_DIR/token.json')
"

# 5. Initialize state files
touch "$CONFIG_DIR/processed.txt"
echo '{}' > "$CONFIG_DIR/thread_sessions.json" 2>/dev/null || true

echo ""
echo "=== Setup Complete ==="
echo "Start the bridge with: $VENV_DIR/bin/python $SCRIPT_DIR/bridge.py start"
echo "Stop with:             $VENV_DIR/bin/python $SCRIPT_DIR/bridge.py stop"
