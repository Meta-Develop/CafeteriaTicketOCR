#!/usr/bin/env bash
set -euo pipefail

TAURI_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$TAURI_DIR/../.." && pwd)"
mode="${1:---build}"
case "$mode" in --build|--check-sdk|--test|--sidecar-check) ;; *) exit 2 ;; esac
command -v cargo >/dev/null || { echo 'Rust/Cargo SDK is missing.' >&2; exit 1; }
command -v pkg-config >/dev/null || { echo 'pkg-config is missing.' >&2; exit 1; }

# Nix development outputs can be used without changing the host configuration.
# Only already-realized paths are inspected; this script never downloads SDKs.
if ! pkg-config --exists gtk+-3.0 webkit2gtk-4.1 javascriptcoregtk-4.1; then
  compiler_root="$(dirname -- "$(dirname -- "$(readlink -f -- "$(command -v cc)")")")"
  compiler_libc=""
  [[ ! -f "$compiler_root/nix-support/orig-libc" ]] || compiler_libc="$(cat "$compiler_root/nix-support/orig-libc")"
  for pc in /nix/store/*webkitgtk*-dev/lib/pkgconfig/webkit2gtk-4.1.pc; do
    [[ -f "$pc" ]] || continue
    # Do not combine a newer WebKit libc ABI with an older installed linker.
    if [[ -n "$compiler_libc" ]]; then
      sdk_libdir="$(sed -n 's/^libdir=//p' "$pc")"
      nix-store --query --references "${sdk_libdir%/lib}" | command grep -Fxq "$compiler_libc" || continue
    fi
    sdk_paths="$("$PROJECT_ROOT/.venv/bin/python" -B - "$pc" <<'PY'
from pathlib import Path
import sys
pending = [Path(sys.argv[1]).parents[2]]
visited = set()
paths = []
while pending:
    package = pending.pop(0)
    if package in visited:
        continue
    visited.add(package)
    for subdir in ('lib/pkgconfig', 'share/pkgconfig'):
        directory = package / subdir
        if directory.is_dir():
            paths.append(str(directory))
    propagated = package / 'nix-support/propagated-build-inputs'
    if propagated.is_file():
        pending.extend(Path(p) for p in propagated.read_text().split())
print(':'.join(paths))
PY
)"
    export PKG_CONFIG_PATH="$sdk_paths${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
    pkg-config --exists gtk+-3.0 webkit2gtk-4.1 javascriptcoregtk-4.1 && break
  done
fi
if ! pkg-config --exists gtk+-3.0 webkit2gtk-4.1 javascriptcoregtk-4.1; then
  echo 'Missing native SDK: GTK 3 >=3.24 and WebKitGTK 4.1 >=2.40 development files.' >&2
  echo 'Provide a project-local SDK environment; no host changes or downloads were attempted.' >&2
  exit 1
fi
if [[ "$mode" == --check-sdk ]]; then
  pkg-config --modversion gtk+-3.0 webkit2gtk-4.1 javascriptcoregtk-4.1
  exit
fi
# pkg-config can suppress a default library search directory even though this
# standalone compiler is outside the Nix development shell that supplied it.
if pkg-config --exists zlib; then
  export LIBRARY_PATH="$(pkg-config --variable=libdir zlib)${LIBRARY_PATH:+:$LIBRARY_PATH}"
fi

if [[ -d /native/local/cache/cargo-targets ]]; then
  build_cache=/native/local/cache/cargo-targets/cafeteria-ticket-ocr-tauri
else
  build_cache="${XDG_CACHE_HOME:-$HOME/.cache}/cafeteria-ticket-ocr/cargo-target"
fi
if [[ -e "$build_cache" ]]; then
  if [[ ! -f "$build_cache/OWNER" ]] || ! head -1 "$build_cache/OWNER" | command grep -Fxq "$PROJECT_ROOT"; then
    echo "Unowned build cache preserved: $build_cache" >&2
    exit 1
  fi
else
  mkdir -p -- "$build_cache"
  printf '%s\n%s\n' "$PROJECT_ROOT" 'CafeteriaTicketOCR native build cache' > "$build_cache/OWNER"
fi
export CARGO_TARGET_DIR="$build_cache"
export CARGO_BUILD_JOBS=1
export CARGO_NET_OFFLINE=true
# A shared compiler daemon can discard the client's niceness. Keep compilation
# in this process tree so every compiler inherits the low-priority build lane.
export RUSTC_WRAPPER=
cd "$TAURI_DIR"
source_config=()
if [[ ! -d "${CARGO_HOME:-$HOME/.cargo}/registry/cache" ]]; then
  "$PROJECT_ROOT/.venv/bin/python" -B "$TAURI_DIR/cached-sources.py" "$build_cache/cached-sources"
  source_config=(--config 'source.crates-io.replace-with="local-cache"' --config "source.local-cache.directory=\"$build_cache/cached-sources\"")
fi
if [[ "$mode" == --test ]]; then
  exec nice -n 19 cargo test "${source_config[@]}" --offline --locked --jobs 1
fi
if [[ "$mode" == --sidecar-check ]]; then
  exec "$build_cache/debug/cafeteria-ticket-ocr" --check-sidecar
fi
nice -n 19 cargo build "${source_config[@]}" --offline --locked --jobs 1
printf '%s\n' "$build_cache/debug/cafeteria-ticket-ocr"
