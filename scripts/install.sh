#!/bin/sh
# Portable remote installer for Linux/macOS/BusyBox-oriented shells.
set -eu

SOURCE=""
PROJECT="."
while [ "$#" -gt 0 ]; do
  case "$1" in
    --source|--source-url)
      [ "$#" -ge 2 ] || { echo "missing value for $1" >&2; exit 2; }
      SOURCE=$2
      shift 2
      ;;
    --project)
      [ "$#" -ge 2 ] || { echo "missing value for --project" >&2; exit 2; }
      PROJECT=$2
      shift 2
      ;;
    -h|--help)
      echo "usage: install.sh --source https://host/skills/coordinator [--project PATH]"
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

[ -n "$SOURCE" ] || {
  echo "--source https://host/skills/coordinator is required when install.sh is piped over curl" >&2
  exit 2
}
SOURCE=${SOURCE%/}
command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required and must be on PATH" >&2
  exit 20
}
command -v git >/dev/null 2>&1 || {
  echo "git is required and must be on PATH" >&2
  exit 20
}

TMP_FILE=$(python3 - <<'PY'
import os
import tempfile
fd, name = tempfile.mkstemp(prefix="coordinator-install-", suffix=".py")
os.close(fd)
print(name)
PY
)
trap 'rm -f "$TMP_FILE"' EXIT HUP INT TERM
python3 - "$SOURCE/scripts/install.py" "$TMP_FILE" <<'PY'
import pathlib
import sys
import urllib.request
url, output = sys.argv[1], pathlib.Path(sys.argv[2])
request = urllib.request.Request(url, headers={"User-Agent": "coordinator-install-sh/2"})
with urllib.request.urlopen(request, timeout=30) as response:
    output.write_bytes(response.read())
PY
python3 "$TMP_FILE" install --source-url "$SOURCE" --project "$PROJECT" --json
