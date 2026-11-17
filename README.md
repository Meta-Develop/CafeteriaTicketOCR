# CafeteriaTicketOCR

A Tauri desktop app (Rust + HTML/CSS/JavaScript) with a Python sidecar for
continuous, real-time Japanese OCR from a USB camera. RapidOCR PP-OCRv4 runs
locally through ONNX Runtime and matches recognized text against a local menu
database. Recognition does not use a cloud service.

Matching normalizes text and common OCR errors, compares menu names against
individual lines and the full text, and uses price and category hints. Weak
or ambiguous matches are rejected.

Automatic ticket detection uses configurable consecutive confirmation passes
(`0` selects adaptive confirmation) and a per-item deduplication gap (default
1.5 seconds). A detected item must be absent from recognition results for that
gap before it can be detected again. Confirmed detections save a full-resolution
PNG, a selected-name text file, and a JSON result containing the OCR text under
`runtime/captures/`. The UI shows current OCR, recognition timing, and detection
history. Exposure, gain, and other supported V4L2 controls apply while dragging;
available ranges and values come from the camera. Recognition can be paused,
and the OCR region and processing scale can be adjusted.

## Layout

| Path | Purpose |
| --- | --- |
| `desktop/src-tauri/` | Rust desktop shell, sidecar bridge, and native build helper |
| `desktop/ui/` | HTML/CSS/JavaScript interface and Node tests |
| `host/` | Python sidecar, USB capture, OCR, menu matching, and unit tests |
| `host/requirements.txt` | Python dependencies |
| `scripts/` | Linux and Windows launchers |
| `data/menu.example.json` | Public sample menu format |
| `data/menu.json` | Private local menu database (ignored) |
| `data/source/` | Private source documents (ignored; not read by the app) |
| `runtime/captures/` | Saved captures and recognition results (ignored) |
| `firmware/` | Separate, optional TimerCamera-F project |

## Local menu

Create `data/menu.json` as a UTF-8 JSON array of objects with `id`, `name`,
`price`, and `type` fields; see [data/menu.example.json](data/menu.example.json)
for sample data. `name` must be nonempty, `id` and `price` are nonnegative
integers, and `price` is in yen. `type` is a text category such as `don`,
`noodle`, or `other`. A nonempty menu is required for recognition readiness.
Keep the real menu, source documents, captures, and OCR results private and
ignored. The app does not read source workbooks directly.

## Linux setup and launch

Provide Python 3.11+, `uv`, Rust/Cargo, a C/C++ build toolchain, `pkg-config`,
GTK 3 (>=3.24), WebKitGTK 4.1 (>=2.40) and JavaScriptCoreGTK 4.1 development
files, and `v4l2-ctl`. The camera path uses Linux V4L2 and requires permission
to access the USB video device.

From the repository root:

```bash
./scripts/run_app.sh --setup
./scripts/run_app.sh --check
./scripts/run_app.sh
```

- `--setup` creates `.venv` with `uv venv --system-site-packages` if needed,
  installs `host/requirements.txt` with `uv pip`, initializes Japanese RapidOCR
  with model downloads permitted, checks OCR/menu readiness, and checks the
  native SDK. It does not open the app or install the native SDK.
- `--check` requires an existing `.venv`, initializes OCR using local models,
  checks OCR/menu readiness, and checks the native SDK. It performs no downloads
  or installs and opens no window or camera.
- Plain run requires `.venv`, builds the native debug binary using
  `desktop/src-tauri/build-native.sh --build`, sets `CAFETERIA_PROJECT_ROOT`,
  and launches it. The helper uses offline, locked Cargo dependencies, one
  build job at low priority, and a build cache outside the checkout. Populate
  the Cargo dependency cache beforehand; Python setup does not fetch crates.

The native helper checks installed SDK files and can reuse already available
Nix store development outputs. It does not invoke `nix-shell` or download SDKs.
`host/app.py` is a compatibility entry point that forwards to the Linux launcher.

## Windows launcher

Provide Python 3.11+, Rust/Cargo, and the native Tauri Windows prerequisites
(Microsoft C++ build tools and WebView2). From the repository root:

```bat
scripts\run.bat --setup
scripts\run.bat --check
scripts\run.bat
```

The batch file creates `.venv` if missing using `py -3`, falling back to `python`
when the launcher is unavailable. Only `--setup` installs `host\requirements.txt`
with pip and permits OCR model downloads. Both `--setup` and `--check` run the
same Python import and OCR/menu readiness check as the Linux launcher, without
opening the app. Plain run executes
`cargo build --manifest-path desktop\src-tauri\Cargo.toml`, sets
`CAFETERIA_PROJECT_ROOT`, and launches the debug executable from
`desktop\src-tauri\target\debug\` (or `CARGO_TARGET_DIR` when set). Cargo may
fetch missing dependencies. The sidecar uses `.venv\Scripts\python.exe`.
Use the batch file on Windows; `host/app.py` prints guidance instead of starting Bash.

The Windows launcher is untested on Windows. Camera discovery, capture, and
controls currently use Linux device paths and V4L2; Windows camera support is
not implemented by this launcher fix.

## Tests

Run from the repository root with dependencies and cached native build inputs
already available. Node.js is required for the UI tests.

```bash
(cd host && ../.venv/bin/python -B -m unittest discover -p 'test_*.py')
(cd desktop/ui && node test/frontend.test.cjs && node test/layout.test.cjs)
bash desktop/src-tauri/build-native.sh --test
```

These checks cover Python logic, a mocked DOM/Tauri bridge, structural layout,
and Rust bridge validation without opening a GUI or a real camera. They do not
establish recognition accuracy on real tickets or hardware acceptance.

## Optional firmware and limitations

[firmware/README.md](firmware/README.md) describes a separate TimerCamera-F
PlatformIO project. It is optional and is not used by the USB-only desktop app.
Keep its WiFi configuration private.

Recognition accuracy on real tickets and school-server integration are
unverified.
