# CafeteriaTicketOCR

Desktop camera OCR prototype for reading cafeteria ticket or menu images and
matching recognized text against known local menu candidates. The Python app
provides camera capture, OCR, candidate selection, and saved OCR transcripts.
The M5Stack TimerCamera-F firmware is a separate companion project.

## Current status

The imported application is an existing local prototype. During this copy,
`host/ocr_menu.py` and the private `data/menu.json` were found to contain
NUL-filled tails beginning at byte 4096. Their original bytes are preserved;
the OCR module cannot currently compile and the private menu cannot be parsed
as JSON. Recover both from a known-good source before launching the app. The
original README also had a damaged tail; this README replaces it in this copy.

This setup does not establish recognition accuracy on physical tickets or any
integration with a school server. GUI, camera, network, and firmware operation
have not been verified for this copy.

## Layout

```text
host/                   Python desktop app and menu matching module
host/requirements.txt   Python dependencies
scripts/                Linux and Windows launchers
firmware/               Separate TimerCamera-F PlatformIO project
data/menu.example.json  Harmless menu format example
data/menu.json          Private local runtime menu (ignored)
data/source/            Private source documents such as XLSX (ignored)
runtime/captures/       Local captures and OCR output (ignored)
runtime/tessdata/       Optional local Tesseract language data (ignored)
```

## Local menu

The app reads `data/menu.json`; it does not read the source XLSX directly.
Keep the copied school menu and source workbook private. To prepare a demo
menu, first preserve any existing `data/menu.json` separately, then copy
`data/menu.example.json` to `data/menu.json` and edit the entries. Use an array
of objects with `id`, `name`, `price`, and `type` fields. This supplies example
data only; recovering the damaged OCR module is still required.

Do not publish the actual menu, source workbook, images, or OCR transcripts
without the data owner's authorization. They are excluded from Git by default.

## Desktop launch

Use Python 3.11 or newer with the packages in `host/requirements.txt`, Tkinter,
and the native libraries required by OpenCV. `rapidocr` is the default engine;
Tesseract and suitable language data are needed for the Tesseract engine.

After restoring the damaged files, the existing Linux launcher supports:

```bash
./scripts/run_app.sh
./scripts/run_app.sh --check
```

The launcher needs `uv` and `nix-shell`. On its first run it creates `.venv`
and installs dependencies; `nix-shell` may fetch native libraries. `--check`
checks imports without opening the GUI, but can still perform that environment
setup. Dependencies and OCR models are not bundled in this copy.

On Windows, run:

```bat
scripts\run.bat
```

That launcher installs dependencies with an available Python interpreter before
opening the app. With an already prepared environment, the direct commands are:

```bash
python -m pip install -r host/requirements.txt
python host/app.py
```

Camera modes in the existing app include USB, TimerCamera-F HTTP, and XIAO
still-image/UDP transports. For TimerCamera-F HTTP mode, supply the camera's
local address; see the firmware documentation for endpoint details.

## Firmware

`firmware/` is an independent PlatformIO project. Run build commands from that
directory and follow [firmware/README.md](firmware/README.md) for hardware,
local WiFi configuration, and HTTP endpoints. No firmware build or flash was
performed during project setup. Keep `firmware/include/local_config.h` local;
only its placeholder example is included.

## Local artifacts

Captures were preserved under `runtime/captures/` as ignored local data.
Virtual environments, PlatformIO output, optional Tesseract data, credentials,
and development context are not bundled. Restore optional dependencies and
local settings separately. Review the candidate Git files before the first
commit or any publication.
