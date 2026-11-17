#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

mode="${1:-}"
case "$mode" in
  ''|--check|--setup) ;;
  *) echo "Usage: $0 [--check|--setup]" >&2; exit 2 ;;
esac

# Dependency and model downloads are explicit; --check stays local and headless.
if [[ "$mode" == "--setup" ]]; then
  if [[ ! -x .venv/bin/python ]]; then
    uv venv --system-site-packages .venv
  fi
  uv pip install --python .venv/bin/python -r host/requirements.txt
elif [[ ! -x .venv/bin/python ]]; then
  echo "Missing project environment. Run ./scripts/run_app.sh --setup first." >&2
  exit 1
fi

if [[ -n "$mode" ]]; then
  PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B - "$mode" <<'PY'
import sys

sys.path.insert(0, "host")
import recognition

recognition.get_rapidocr_engine(allow_download=sys.argv[1] == "--setup")
problem = recognition.ocr_unavailable_reason(recognition.DEFAULT_OCR_ENGINE)
if problem:
    raise SystemExit(f"app_check: {problem}")
from ocr_menu import load_menu_items

print(f"app_check: ok (Japanese RapidOCR, {len(load_menu_items(recognition.MENU_DATABASE_PATH))} menu items)")
PY
  bash desktop/src-tauri/build-native.sh --check-sdk
else
  native_binary="$(bash desktop/src-tauri/build-native.sh --build)"
  export CAFETERIA_PROJECT_ROOT="$REPO_ROOT"
  exec "$native_binary"
fi
