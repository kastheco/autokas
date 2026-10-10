#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi

# Record only successful installs so an interrupted setup can be run again.
if ! cmp -s requirements.txt .venv/.test-requirements.txt; then
  .venv/bin/python -m pip install -r requirements.txt
  cp requirements.txt .venv/.test-requirements.txt
fi

if [[ ! -e config.json ]]; then
  cp config.example.json config.json
fi
