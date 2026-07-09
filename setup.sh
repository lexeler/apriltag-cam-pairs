#!/usr/bin/env bash
# Create the virtual environment and install dependencies. Run once.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Virtual environment (.venv)"
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip wheel >/dev/null

echo "==> Installing dependencies (requirements.txt)"
./.venv/bin/pip install -r requirements.txt

echo
echo "Setup complete. Next steps:"
echo "  1) cp config.example.yaml config.yaml    — set cameras and credentials"
echo "  2) export CAM_PASSWORD='<password>'       — when using \${CAM_PASSWORD}"
echo "  3) ./.venv/bin/python cam_pairs.py --pretty"
