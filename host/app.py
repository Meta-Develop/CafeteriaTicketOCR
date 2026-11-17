"""Compatibility entry point for the native USB camera application."""
from __future__ import annotations

import os
from pathlib import Path
import sys

# Older --check/--setup launchers use these names. Importing stays side-effect free.
from recognition import (
    DEFAULT_OCR_ENGINE, MENU_DATABASE_PATH, get_rapidocr_engine, ocr_unavailable_reason,
)


def main() -> None:
    if sys.platform == "win32":
        raise SystemExit("On Windows, use scripts\\run.bat [--setup|--check] to launch or check the desktop app.")
    launcher = Path(__file__).resolve().parent.parent / "scripts" / "run_app.sh"
    os.execv("/usr/bin/env", ["env", "bash", str(launcher), *sys.argv[1:]])


if __name__ == "__main__":
    main()
