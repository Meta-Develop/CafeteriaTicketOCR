"""Bounded newline-JSON service for the native Tauri shell (stdout is protocol)."""
from __future__ import annotations

from collections import deque
import json
import math
import os
import re
import sys
import threading
import time
import uuid

# Apply limits before importing numerical runtimes. No model or camera is opened here.
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[_name] = "1"

from camera_stream import CameraStream, list_devices
from recognition import capture_and_recognize, ocr_status
from live_recognition import LiveRecognition, validate_live


class Outbox:
    """Replies/results are bounded and ordered; preview/state overwrite old values."""
    def __init__(self, stream):
        self.stream = stream
        self.condition = threading.Condition()
        self.messages = deque()
        self.latest = {}
        self.closed = False
        self.failed = False
        self.writer = threading.Thread(target=self._write, name="json-writer", daemon=True)
        self.writer.start()

    def send(self, message):
        with self.condition:
            deadline = time.monotonic() + 1.0
            while len(self.messages) >= 64 and not self.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.closed = self.failed = True
                    self.condition.notify_all()
                    raise BrokenPipeError("protocol consumer stalled")
                self.condition.wait(remaining)
            if self.closed:
                raise BrokenPipeError("protocol closed")
            self.messages.append(message)
            self.condition.notify_all()

    def emit(self, name, payload):
        message = {"event": name, "payload": payload}
        if name in {"recognition_result", "ticket_detected", "live_recognition"}:
            self.send(message)
        else:
            with self.condition:
                if not self.closed:
                    if name == "camera_state" and not payload["connected"]:
                        self.latest.pop("camera_frame", None)
                    self.latest[name] = message
                    self.condition.notify_all()

    def _write(self):
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.messages or self.latest or self.closed)
                    if self.messages:
                        message = self.messages.popleft()
                    elif self.latest:
                        key = "camera_state" if "camera_state" in self.latest else next(iter(self.latest))
                        message = self.latest.pop(key)
                    else:
                        return
                    self.condition.notify_all()
                self.stream.write(json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
                self.stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            with self.condition:
                self.closed = self.failed = True
                self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.latest.clear()
            self.condition.notify_all()
        self.writer.join(timeout=0.5)


_ACTION_FIELDS = {
    "status": set(), "start_camera": {"device"}, "stop_camera": set(),
    "set_camera": {"exposure_mode", "exposure_us", "gain", "controls"},
    "set_live": {"enabled", "confirm_passes", "dedup_gap", "roi", "scale"},
    "recognize": set(), "shutdown": set(),
}


def validate_request(request):
    if not isinstance(request, dict):
        raise ValueError("リクエストはJSONオブジェクトで指定してください。")
    request_id = request.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, int) or not 0 <= request_id <= 2**53 - 1:
        raise ValueError("リクエストIDが無効です。")
    action = request.get("action")
    if not isinstance(action, str) or action not in _ACTION_FIELDS:
        raise ValueError("対応していない操作です。")
    if set(request) - ({"id", "action"} | _ACTION_FIELDS[action]):
        raise ValueError("対応していない設定項目があります。")
    if "device" in request and (not isinstance(request["device"], str) or len(request["device"]) > 512):
        raise ValueError("USBカメラの指定が無効です。")
    if "exposure_mode" in request and request["exposure_mode"] not in ("short", "auto", "manual"):
        raise ValueError("露出モードが無効です。")
    for key in ("exposure_us", "gain"):
        if key in request:
            value = request[key]
            minimum = -1_000_000_000 if key == "gain" else 0
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= 1_000_000_000 or not math.isfinite(value):
                raise ValueError("露出・ゲインには有効な数値を指定してください。")
    if action == "set_camera" and not (_ACTION_FIELDS[action] & set(request)):
        raise ValueError("変更するカメラ設定を指定してください。")
    if request.get("exposure_mode") == "manual" and "exposure_us" not in request:
        raise ValueError("手動露出の時間を指定してください。")
    if "controls" in request:
        controls = request["controls"]
        if (not isinstance(controls, dict) or not 1 <= len(controls) <= 32
                or any(not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
                       or type(value) is not int or not -1_000_000_000 <= value <= 1_000_000_000
                       for key, value in controls.items())):
            raise ValueError("カメラ設定の指定が無効です。")
    if action == "set_live":
        validate_live({key: value for key, value in request.items() if key in _ACTION_FIELDS[action]})
    return action


class BackendService:
    def __init__(self, outbox, *, camera=None, devices=list_devices, readiness=ocr_status,
                 recognize=capture_and_recognize, live=None):
        self.outbox = outbox
        self.camera = camera if camera is not None else CameraStream(outbox.emit, devices=devices)
        self.devices = devices
        self.readiness = readiness
        self.recognize = recognize
        self.busy = False
        self.closing = False
        self.job = None
        self.lock = threading.Lock()
        self.live = live if live is not None else LiveRecognition(self.camera, outbox.emit, readiness=readiness)
        self.live.start()

    def status(self):
        with self.lock:
            busy = self.busy
        return {"camera": self.camera.snapshot(), "devices": self.devices(), "ocr": self.readiness(),
                "busy": busy, "live": self.live.snapshot()}

    def handle(self, request):
        request_id = request.get("id") if isinstance(request, dict) else None
        if isinstance(request_id, bool) or not isinstance(request_id, int) or not 0 <= request_id <= 2**53 - 1:
            request_id = None
        work = None
        try:
            action = validate_request(request)
            if self.closing:
                raise ValueError("アプリを終了しています。")
            if action == "status":
                result = self.status()
            elif action == "start_camera":
                self.camera.start(request.get("device"))
                result = self.status()
            elif action == "stop_camera":
                self.camera.stop()
                result = self.status()
            elif action == "set_camera":
                result = self.camera.set_controls({key: value for key, value in request.items() if key in _ACTION_FIELDS[action]})
            elif action == "set_live":
                result = self.live.configure({key: value for key, value in request.items() if key in _ACTION_FIELDS[action]})
            elif action == "recognize":
                if self.live.snapshot()["enabled"]:
                    raise ValueError("ライブ認識を一時停止してから撮影してください。")
                with self.lock:
                    if self.busy:
                        raise ValueError("認識中です。結果を待ってから撮影してください。")
                    ready = self.readiness()
                    if not ready["ready"]:
                        raise ValueError(ready["error"] or "日本語認識を利用できません。")
                    frame = self.camera.latest_frame()
                    if frame is None:
                        raise ValueError("映像がありません。USBカメラの接続と明るさを確認してください。")
                    self.busy = True
                capture_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:12]
                result = {"accepted": True, "capture_id": capture_id}
                work = (frame, capture_id)
            else:
                self.closing = True
                result = {"accepted": True}
            self.outbox.send({"id": request_id, "ok": True, "result": result})
            # Only start after the acceptance reply is enqueued, even for instant jobs.
            if work is not None:
                self.job = threading.Thread(target=self._recognize, args=work, daemon=True, name="ocr-job")
                self.job.start()
        except ValueError as exc:
            self.outbox.send({"id": request_id, "ok": False, "error": str(exc)})
        except BrokenPipeError:
            self.closing = True
        except Exception as exc:
            print(f"backend request failed: {type(exc).__name__}", file=sys.stderr)
            self.outbox.send({"id": request_id, "ok": False, "error": "操作に失敗しました。接続を確認して、もう一度お試しください。"})

    def _recognize(self, frame, capture_id):
        try:
            result = self.recognize(frame, capture_id)
        except Exception as exc:
            print(f"recognition job failed: {type(exc).__name__}", file=sys.stderr)
            result = {"capture_id": capture_id, "status": "error", "name": None, "price": None,
                      "score": None, "text": "", "confidence": None, "elapsed_ms": 0,
                      "message": "認識できませんでした。もう一度お試しください。"}
        with self.lock:
            self.busy = False
        try:
            self.outbox.emit("recognition_result", result)
        except BrokenPipeError:
            self.closing = True

    def close(self):
        self.closing = True
        self.live.close()
        self.camera.stop()
        if self.job:
            self.job.join(timeout=1.0)


def serve(input_stream, output_stream):
    import cv2
    cv2.setNumThreads(1)
    outbox = Outbox(output_stream)
    service = BackendService(outbox)
    try:
        while not service.closing and not outbox.failed:
            line = input_stream.readline(4097)
            if not line:
                break
            if len(line) > 4096:
                while line and not line.endswith("\n"):
                    line = input_stream.readline(4097)
                outbox.send({"id": None, "ok": False, "error": "リクエストが長すぎます。"})
                continue
            try:
                request = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, RecursionError):
                outbox.send({"id": None, "ok": False, "error": "JSONリクエストを読み取れません。"})
                continue
            service.handle(request)
    except (BrokenPipeError, OSError):
        pass
    finally:
        service.close()
        outbox.close()


if __name__ == "__main__":
    serve(sys.stdin, sys.stdout)
