#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
exec .venv/bin/python -m refmod_builder.ui "$@"
