#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -x .venv/bin/python ]]; then
  uv venv --system-site-packages .venv
  uv pip install --python .venv/bin/python -r host/requirements.txt
fi

packages=(
  stdenv.cc.cc.lib
  zlib
  glib
  libGL
  libxcrypt
  xorg.libxcb
  xorg.libX11
  xorg.libXext
  xorg.libXrender
  xorg.libSM
  xorg.libICE
)

if [[ "${1:-}" == "--check" ]]; then
  run_cmd='export LD_LIBRARY_PATH="$(printf "%s\n" $NIX_LDFLAGS | sed -n "s/^-L//p" | paste -sd: -)${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"; .venv/bin/python -c "import sys; sys.path.insert(0, \"host\"); import pytesseract, PIL, cv2, numpy, serial, rapidocr, onnxruntime; import app; print(\"app_check: ok\")"'
else
  run_cmd='export LD_LIBRARY_PATH="$(printf "%s\n" $NIX_LDFLAGS | sed -n "s/^-L//p" | paste -sd: -)${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"; exec .venv/bin/python host/app.py'
fi

exec nix-shell -p "${packages[@]}" --run "$run_cmd"
