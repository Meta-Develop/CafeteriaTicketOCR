"""USB camera discovery and capability-checked Linux controls.

OpenCV owns streaming; v4l2-ctl supplies native control ranges and readback.
No OpenCV exposure scales are assumed for other backends.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


USB_PRESETS = {
    "1280x720 MJPG 120 fps": (1280, 720, 120.0),
    "640x480 MJPG 120 fps": (640, 480, 120.0),
}
USB_DEFAULT_PRESET = next(iter(USB_PRESETS))


@dataclass(frozen=True)
class UsbControls:
    auto: bool = False
    exposure: str = "min"
    gain: str = "0"


@dataclass(frozen=True)
class UsbCaptureSettings:
    width: int = 1280
    height: int = 720
    fps: float = 120.0
    controls: UsbControls = field(default_factory=UsbControls)


@dataclass
class Control:
    name: str
    kind: str
    minimum: int | None
    maximum: int | None
    step: int
    value: int | None
    flags: str
    choices: dict[int, str] = field(default_factory=dict)

    def requested_value(self, text: str) -> int:
        if self.minimum is None or self.maximum is None:
            raise ValueError(f"{self.name}: camera did not report a range")
        value = self.minimum if text.strip().lower() == "min" else int(text)
        if not self.minimum <= value <= self.maximum or (value - self.minimum) % self.step:
            raise ValueError(
                f"{self.name}: expected {self.minimum}..{self.maximum}, step {self.step}"
            )
        return value


def parse_controls(output: str) -> dict[str, Control]:
    controls: dict[str, Control] = {}
    current: Control | None = None
    for line in output.splitlines():
        match = re.match(r"\s*(\w+)\s+0x[0-9a-fA-F]+\s+\(([^)]+)\)\s*:\s*(.*)", line)
        if match:
            name, kind, details = match.groups()
            numbers = {key: int(value) for key, value in re.findall(r"(\w+)=(-?\d+)", details)}
            flags = details.partition("flags=")[2]
            current = Control(
                name, kind, numbers.get("min", 0 if kind == "bool" else None),
                numbers.get("max", 1 if kind == "bool" else None),
                max(1, numbers.get("step", 1)), numbers.get("value"), flags,
            )
            controls[name] = current
        elif current is not None:
            choice = re.match(r"\s+(-?\d+):\s*(.+)", line)
            if choice:
                current.choices[int(choice[1])] = choice[2].strip()
    return controls


def discover_usb_sources(directory: Path = Path("/dev/v4l/by-id")) -> list[str]:
    """List stable capture nodes without opening them; prefer external cameras."""
    paths = [p for p in directory.glob("*-video-index0") if p.exists()]
    return [str(p) for p in sorted(paths, key=lambda p: (
        bool(re.search(r"integrated|built.?in|internal|facetime", p.name, re.I)), p.name
    ))]


def parse_usb_source(text: str, platform: str = sys.platform) -> int | str:
    value = text.strip()
    if value.isdecimal():
        return int(value)
    if platform.startswith("linux"):
        path = Path(value)
        if (re.fullmatch(r"/dev/video\d+", value) or
                (path.parent == Path("/dev/v4l/by-id") and path.name not in {".", ".."})):
            return value
    raise ValueError("USB source must be a nonnegative index, /dev/videoN, or /dev/v4l/by-id path.")


class V4L2Controls:
    def __init__(self, source: int | str) -> None:
        self.device = f"/dev/video{source}" if isinstance(source, int) else source

    def _run(self, option: str) -> str:
        try:
            result = subprocess.run(
                ["v4l2-ctl", f"--device={self.device}", option],
                capture_output=True, text=True, timeout=2, check=False,
                env={**os.environ, "LC_ALL": "C"},
            )
        except FileNotFoundError as exc:
            raise RuntimeError("USB controls unavailable: v4l2-ctl is not installed") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"USB controls unavailable: {exc}") from exc
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or "v4l2-ctl failed")
        return result.stdout

    def query(self) -> dict[str, Control]:
        return parse_controls(self._run("--list-ctrls-menus"))

    def apply(self, request: UsbControls) -> str:
        controls = self.query()
        errors: list[str] = []
        expected: dict[str, int] = {}

        def find(*names: str) -> Control | None:
            return next((controls[name] for name in names if name in controls), None)

        def write(control: Control | None, value: int, label: str) -> bool:
            if control is None:
                errors.append(f"{label}: unsupported")
                return False
            if "disabled" in control.flags or "read-only" in control.flags or "inactive" in control.flags:
                errors.append(f"{control.name}: {control.flags}")
                return False
            try:
                self._run(f"--set-ctrl={control.name}={value}")
                expected[control.name] = value
                return True
            except RuntimeError as exc:
                errors.append(f"{control.name}: {exc}")
                return False

        auto = find("auto_exposure", "exposure_auto")
        desired_mode = None
        if auto is not None:
            labels = ("aperture priority mode", "auto mode") if request.auto else ("manual mode",)
            for label in labels:
                desired_mode = next((key for key, text in auto.choices.items() if text.lower() == label), None)
                if desired_mode is not None:
                    break
        if desired_mode is None:
            errors.append("auto exposure: requested mode unsupported")
        elif write(auto, desired_mode, "auto exposure"):
            controls = self.query()  # Manual exposure may now become active.

        exposure = find("exposure_time_absolute", "exposure_absolute")
        if not request.auto:
            mode_readback = controls.get(auto.name) if auto is not None else None
            if mode_readback is None or desired_mode is None or mode_readback.value != desired_mode:
                errors.append("manual exposure skipped: manual mode not confirmed")
            elif exposure is None:
                errors.append("exposure: unsupported")
            else:
                try:
                    write(exposure, exposure.requested_value(request.exposure), "exposure")
                except ValueError as exc:
                    errors.append(str(exc))
        gain = find("gain")
        if request.gain.strip():
            if gain is None:
                errors.append("gain: unsupported")
            else:
                try:
                    write(gain, gain.requested_value(request.gain), "gain")
                except ValueError as exc:
                    errors.append(str(exc))
        dynamic = find("exposure_dynamic_framerate", "exposure_auto_priority")
        if dynamic is not None:
            write(dynamic, 0, "dynamic framerate")

        actual = self.query()
        for name, value in expected.items():
            readback = actual.get(name)
            if readback is None or readback.value != value:
                errors.append(f"{name}: requested {value}, readback {readback.value if readback else 'missing'}")
        values = []
        for control in (auto, exposure, gain, dynamic):
            if control is None:
                continue
            current = actual.get(control.name)
            if current is None:
                continue
            value = current.value
            detail = f"{control.name}={value}"
            if current.choices:
                detail += f" ({current.choices.get(value, 'unknown mode')})"
            if current.name in {"exposure_time_absolute", "exposure_absolute"}:
                detail += f" ({value * 0.1:g} ms)" if value is not None else ""
            if current.minimum is not None:
                detail += f" [{current.minimum}..{current.maximum}; step {current.step}]"
            values.append(detail)
        return " | ".join(values + (["ERROR: " + "; ".join(errors)] if errors else []))
