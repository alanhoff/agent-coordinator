#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
command -v python3 >/dev/null 2>&1 || { echo "Python 3.11+ is required" >&2; exit 20; }
exec python3 "$SCRIPT_DIR/install.py" "$@"
