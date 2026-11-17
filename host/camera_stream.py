"""USB-only asynchronous capture with latest frames and native V4L2 readback."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import threading
import time
import uuid

import cv2

from usb_camera import UsbControls, V4L2Controls, discover_usb_sources, parse_usb_source


def empty_camera() -> dict:
    return {"connected": False, "session_id": None, "device": None, "stream_fps": 0.0, "preview_fps": 0.0,
            "width": 0, "height": 0, "exposure_us": None, "min_exposure_us": None,
            "gain": None, "gain_min": None, "gain_max": None, "auto_exposure": False,
            "brightness": None, "error": None}


def list_devices() -> list[dict]:
    sources = discover_usb_sources()
    resolved = {str(Path(source).resolve()) for source in sources}
    # A by-id path may be absent on some UVC cameras. Skip metadata nodes (index 1).
    for node in sorted(Path("/sys/class/video4linux").glob("video*")):
        device = f"/dev/{node.name}"
        try:
            if (node / "index").read_text().strip() == "0" and device not in resolved and Path(device).exists():
                sources.append(device)
        except OSError:
            continue
    devices = []
    for source in sources:
        node = Path(source).resolve().name
        try:
            label = (Path("/sys/class/video4linux") / node / "name").read_text().strip()
        except OSError:
            label = Path(source).name
        devices.append({"id": source, "label": f"{label} ({node})"})
    return devices


def device_in_use(source: str) -> bool:
    """Check observable device holders before opening; never interrupt another owner."""
    try:
        identity = Path(source).stat().st_rdev
    except OSError:
        return False
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal() or int(proc.name) == os.getpid():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                try:
                    stat = fd.stat()
                    if stat.st_rdev == identity and stat.st_rdev != 0:
                        return True
                except OSError:
                    continue
        except (OSError, PermissionError):
            continue
    return False


def _control(controls, *names):
    return next((controls[name] for name in names if name in controls), None)


def _clamp(control, requested):
    if control is None or control.minimum is None or control.maximum is None:
        return None
    value = min(control.maximum, max(control.minimum, requested))
    index = min((control.maximum - control.minimum) // control.step, round((value - control.minimum) / control.step))
    return control.minimum + index * control.step


class CameraStream:
    def __init__(self, emit, *, capture_factory=None, controls_factory=V4L2Controls,
                 devices=list_devices, owner_check=device_in_use, freeze_seconds=2.0):
        self.emit = emit
        self.capture_factory = capture_factory or cv2.VideoCapture
        self.controls_factory = controls_factory
        self.devices = devices
        self.owner_check = owner_check
        self.freeze_seconds = freeze_seconds
        self.lock = threading.RLock()
        self.controls_lock = threading.Lock()
        self.active_controls = None
        self.read_started = 0.0
        self.state = empty_camera()
        self.session_id = None
        self.frame = None
        self.frame_time = 0.0
        self.stop_event = threading.Event()
        self.restart_event = threading.Event()
        self.thread = None
        self.monitor = None
        self.pending = None
        self.settings = {"exposure_mode": "short", "gain": 24}
        self.last_state = 0.0

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _state(self, *, important=False, session=None, **changes):
        now = time.monotonic()
        with self.lock:
            if session is not None and session != self.session_id:
                return
            if changes.get("connected") is False:
                self.session_id = None
                self.frame = None
                changes["session_id"] = None
            self.state.update(changes)
            if not important and now - self.last_state < 0.5:
                return
            self.last_state = now
            state = dict(self.state)
            self.emit("camera_state", state)

    def latest_frame(self):
        with self.lock:
            if not self.state["connected"] or self.frame is None or time.monotonic() - self.frame_time > self.freeze_seconds:
                return None
            return self.frame.copy()

    def start(self, device=None):
        available = self.devices()
        if device is None:
            if not available:
                raise ValueError("USBカメラがありません。接続して、もう一度お試しください。")
            device = available[0]["id"]
        if device not in {item["id"] for item in available}:
            raise ValueError("選択したUSBカメラが見つかりません。接続を確認してください。")
        parse_usb_source(device)
        if self.thread and self.thread.is_alive():
            if self.snapshot()["device"] == device and not self.stop_event.is_set():
                return
            if not self.stop():
                raise ValueError("前のカメラを解放中です。少し待ってから再接続してください。")
        if self.monitor and self.monitor.is_alive():
            self.stop_event.set()
            self.monitor.join(timeout=0.3)
        with self.lock:
            self.stop_event = threading.Event()
            self.restart_event = threading.Event()
            self.session_id = None
            self.frame = None
            self.state = {**empty_camera(), "device": device}
            self.pending = None
            self.read_started = 0.0
            self._state(important=True)
        self.thread = threading.Thread(target=self._run, args=(device, self.stop_event, self.restart_event), daemon=True, name="usb-capture")
        self.monitor = threading.Thread(target=self._watch, args=(self.stop_event, self.restart_event), daemon=True, name="usb-watch")
        self.thread.start()
        self.monitor.start()

    def stop(self):
        with self.lock:
            self.stop_event.set()
            self._state(important=True, connected=False, stream_fps=0.0, preview_fps=0.0)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=0.5)
        released = not (self.thread and self.thread.is_alive())
        self._state(important=True, connected=False, stream_fps=0.0, preview_fps=0.0,
                    error=None if released else "カメラの応答を待っています。解放後に再接続してください。")
        return released

    def set_controls(self, request):
        if not self.thread or not self.thread.is_alive() or self.stop_event.is_set():
            raise ValueError("USBカメラを接続してから設定してください。")
        with self.lock:
            self.settings.update(request)
            self.pending = dict(self.settings)
        return {"accepted": True}

    def _watch(self, stop_event, restart_event):
        next_readback = time.monotonic() + 1.0
        while not stop_event.wait(0.25):
            now = time.monotonic()
            with self.lock:
                if stop_event.is_set() or stop_event is not self.stop_event:
                    return
                session = self.session_id
                stale = ((self.state["connected"] and now - self.frame_time > self.freeze_seconds)
                         or (self.read_started and now - self.read_started > self.freeze_seconds))
                if stale and not restart_event.is_set():
                    restart_event.set()
                    self._state(important=True, session=session, connected=False, stream_fps=0.0, preview_fps=0.0,
                                error="カメラの映像が止まりました。USB接続を確認してください。自動で再接続します。")
                connected = self.state["connected"]
            if now >= next_readback and connected and self.controls_lock.acquire(blocking=False):
                try:
                    if self.active_controls is not None:
                        actual = self.active_controls.query()
                        if not stop_event.is_set():
                            self._read_controls(actual, self.snapshot()["error"], important=False, session=session)
                except (RuntimeError, OSError, ValueError):
                    if not stop_event.is_set():
                        self._state(session=session, error="露出・ゲインの読戻しを取得できません。")
                finally:
                    self.controls_lock.release()
                next_readback = now + 1.0

    def _read_controls(self, controls, error=None, *, important=True, session=None):
        exposure = _control(controls, "exposure_time_absolute", "exposure_absolute")
        gain = _control(controls, "gain")
        auto = _control(controls, "auto_exposure", "exposure_auto")
        manual = next((value for value, label in auto.choices.items() if label.lower() == "manual mode"), None) if auto else None
        self._state(important=important, session=session,
                    exposure_us=exposure.value * 100 if exposure and exposure.value is not None else None,
                    min_exposure_us=exposure.minimum * 100 if exposure and exposure.minimum is not None else None,
                    gain=gain.value if gain else None, gain_min=gain.minimum if gain else None,
                    gain_max=gain.maximum if gain else None,
                    auto_exposure=bool(auto and auto.value is not None and manual is not None and auto.value != manual),
                    error=error)

    def _apply(self, controls, request, *, session=None):
        with self.controls_lock:
            self._apply_locked(controls, request, session=session)

    def _apply_locked(self, controls, request, *, session=None):
        if controls is None:
            return
        try:
            ranges = controls.query()
            gain = _clamp(_control(ranges, "gain"), request.get("gain", 24))
            exposure = _clamp(_control(ranges, "exposure_time_absolute", "exposure_absolute"), request.get("exposure_us", 100) / 100)
            mode = request["exposure_mode"]
            report = controls.apply(UsbControls(auto=mode == "auto",
                exposure="min" if mode == "short" else str(int(exposure)) if exposure is not None else "min",
                gain=str(int(gain)) if gain is not None else ""))
            actual = controls.query()
            error = "このカメラでは一部の露出・ゲイン設定を適用できません。対応する設定で続行してください。" if "ERROR:" in report else None
            if gain is None and "gain" in request:
                error = "このカメラではゲインを調整できません。"
            self._read_controls(actual, error, session=session)
        except (RuntimeError, OSError, ValueError):
            self._state(important=True, session=session, exposure_us=None, min_exposure_us=None, gain=None,
                        gain_min=None, gain_max=None, auto_exposure=False,
                        error="露出・ゲインを取得できません。カメラの対応状況と v4l2-ctl を確認してください。")

    def _run(self, device, stop_event, restart_event):
        while not stop_event.is_set():
            capture = None
            with self.lock:
                if stop_event.is_set() or stop_event is not self.stop_event:
                    return
                session = self.session_id = uuid.uuid4().hex
                restart_event.clear()
            try:
                if self.owner_check(device):
                    raise RuntimeError("他のアプリがカメラを使用しています。そのアプリでカメラを解放してください。")
                capture = self.capture_factory(parse_usb_source(device), cv2.CAP_V4L2)
                if not capture.isOpened():
                    raise RuntimeError("カメラを開けません。USB接続・アクセス権限・他のアプリの使用状況を確認してください。")
                capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                capture.set(cv2.CAP_PROP_FPS, 120)
                capture.set(cv2.CAP_PROP_BUFFERSIZE, 2)
                controls = self.controls_factory(device)
                with self.controls_lock:
                    self.active_controls = controls
                self._apply(controls, dict(self.settings), session=session)
                started = time.monotonic()
                frames = previews = 0
                brightness = None
                next_preview = 0.0
                while not stop_event.is_set() and not restart_event.is_set():
                    with self.lock:
                        request, self.pending = self.pending, None
                    if request is not None:
                        self._apply(controls, request, session=session)
                    with self.lock:
                        self.read_started = time.monotonic()
                    ok, frame = capture.read()
                    with self.lock:
                        self.read_started = 0.0
                    if stop_event.is_set() or restart_event.is_set():
                        break
                    if not ok or frame is None or not frame.size:
                        raise RuntimeError("カメラの映像を取得できません。USB接続を確認してください。自動で再接続します。")
                    now = time.monotonic()
                    height, width = frame.shape[:2]
                    with self.lock:
                        if session != self.session_id:
                            break
                        self.frame, self.frame_time = frame, now
                    frames += 1
                    payload = None
                    if now >= next_preview:
                        scale = min(640 / width, 360 / height, 1.0)
                        preview = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))))
                        brightness = round(float(cv2.cvtColor(preview, cv2.COLOR_BGR2GRAY).mean()), 1)
                        ok, encoded = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 75])
                        if ok:
                            payload = {"jpeg": base64.b64encode(encoded).decode("ascii"), "session_id": session,
                                      "width": preview.shape[1], "height": preview.shape[0]}
                            previews += 1
                        next_preview = time.monotonic() + 1 / 30
                    elapsed = max(now - started, 0.001)
                    with self.lock:
                        if session != self.session_id:
                            break
                        self._state(important=not self.state["connected"], session=session,
                                    connected=True, session_id=session, width=width, height=height, brightness=brightness,
                                    stream_fps=round(frames / elapsed, 1) if elapsed >= 0.1 else 0.0,
                                    preview_fps=round(previews / elapsed, 1) if elapsed >= 0.1 else 0.0)
                        if payload is not None:
                            payload["timestamp_ms"] = round(time.time() * 1000)
                            self.emit("camera_frame", payload)
                            # Encoding/control readback can take longer than a frame.
                            # Space deliveries from the actual send, without delaying capture.
                            next_preview = time.monotonic() + 1 / 30
                    if elapsed >= 1:
                        started, frames, previews = now, 0, 0
            except Exception as exc:
                message = str(exc) if isinstance(exc, RuntimeError) else "カメラでエラーが発生しました。USB接続とアクセス権限を確認してください。"
                self._state(important=True, session=session, connected=False, stream_fps=0.0, preview_fps=0.0, error=message)
            finally:
                with self.controls_lock:
                    self.active_controls = None
                if capture is not None:
                    capture.release()
                with self.lock:
                    self.read_started = 0.0
                    self._state(important=True, session=session, connected=False, stream_fps=0.0, preview_fps=0.0)
            if not stop_event.is_set():
                stop_event.wait(1.0)
