"""One OCR consumer, no frame queue; only confirmed tickets create captures."""
from __future__ import annotations

from collections import deque
import math
import threading
import time
import uuid

import cv2

from recognition import capture_and_recognize, ocr_status, recognize_image


DEFAULTS = {"enabled": True, "confirm_passes": 0, "dedup_gap": 1.5,
            "roi": 1.0, "scale": 1.0}


def validate_live(settings):
    if not isinstance(settings, dict) or not settings or set(settings) - DEFAULTS.keys():
        raise ValueError("ライブ認識の設定項目が無効です。")
    for key, value in settings.items():
        if key == "enabled":
            valid = isinstance(value, bool)
        elif key == "confirm_passes":
            valid = type(value) is int and 0 <= value <= 10
        else:
            bounds = {"dedup_gap": (0.1, 30), "roi": (0.1, 1), "scale": (0.1, 1)}[key]
            valid = (type(value) in (int, float) and math.isfinite(value)
                     and bounds[0] <= value <= bounds[1])
        if not valid:
            raise ValueError("ライブ認識の設定値が範囲外です。")
    return settings


class TicketDetector:
    """Consecutive confirmations and per-item observed-absence suppression."""
    def __init__(self):
        self.reported = {}
        self.candidate = None
        self.count = 0
        self.last_pass = None

    def observe(self, result, now, confirm_passes=0, dedup_gap=1.5):
        key = (result["name"], result.get("price")) if result["status"] == "matched" else None
        for item, absent_since in list(self.reported.items()):
            if absent_since is not None and now - absent_since >= dedup_gap:
                del self.reported[item]
            elif item != key and absent_since is None:
                self.reported[item] = now
        if key in self.reported:
            self.reported[key] = None
        recent = self.last_pass is not None and now - self.last_pass <= dedup_gap
        self.count = self.count + 1 if key and key == self.candidate and recent else 1 if key else 0
        self.candidate, self.last_pass = key, now
        # Zero selects adaptive confirmation; explicit counts always win.
        strong = result.get("exact_name") or (result.get("score") or 0) >= 0.95
        required = confirm_passes or (1 if strong else 2)
        if key and key not in self.reported and self.count >= required:
            self.reported[key] = None
            return True
        return False


class LiveRecognition:
    def __init__(self, camera, emit, *, recognize=recognize_image,
                 save=capture_and_recognize, readiness=ocr_status, clock=time.monotonic):
        self.camera, self.emit = camera, emit
        self.recognize, self.save, self.readiness, self.clock = recognize, save, readiness, clock
        self.lock = threading.RLock()
        self.settings = dict(DEFAULTS)
        self.revision = 0
        self.running = False
        self.error = None
        self.stop_event = threading.Event()
        self.thread = None

    def snapshot(self):
        with self.lock:
            return {**self.settings, "running": self.running, "error": self.error}

    def configure(self, settings):
        validate_live(settings)
        with self.lock:
            self.settings.update(settings)
            self.revision += 1
            self.running = False
            self.error = None
            self.emit("live_state", self.snapshot())
            return {"accepted": True, **self.snapshot()}

    def start(self):
        self.thread = threading.Thread(target=self._run, name="live-ocr", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        with self.lock:
            self.revision += 1
        if self.thread:
            self.thread.join(timeout=1)

    def _state(self, running, error=None):
        with self.lock:
            if (self.running, self.error) != (running, error):
                self.running, self.error = running, error
                self.emit("live_state", self.snapshot())

    def _run(self):
        last_frame = None
        session = None
        detector = TicketDetector()
        passes = deque(maxlen=60)
        checked_at, ready = -math.inf, False
        seen_revision = -1
        while not self.stop_event.is_set():
            try:
                with self.lock:
                    settings, revision = dict(self.settings), self.revision
                if seen_revision != revision:
                    detector.candidate, detector.count = None, 0
                    passes.clear()
                    seen_revision = revision
                if not settings["enabled"] or not self.camera.snapshot()["connected"]:
                    self._state(False)
                    self.stop_event.wait(0.05)
                    continue
                now = self.clock()
                if now - checked_at >= 1:
                    status = self.readiness()
                    checked_at, ready = now, status["ready"]
                    if not ready:
                        self._state(False, status["error"])
                if not ready:
                    self.stop_event.wait(0.1)
                    continue
                sample = self.camera.latest_frame(after=last_frame, with_metadata=True)
                if sample is None:
                    self.stop_event.wait(0.01)
                    continue
                frame, last_frame, current_session = sample
                if session != current_session:
                    session, detector = current_session, TicketDetector()
                    passes.clear()
                started = self.clock()
                height, width = frame.shape[:2]
                rh, rw = max(1, int(height * settings["roi"])), max(1, int(width * settings["roi"]))
                y, x = (height - rh) // 2, (width - rw) // 2
                image = frame[y:y + rh, x:x + rw].copy()
                if settings["scale"] != 1:
                    image = cv2.resize(image, (max(1, int(rw * settings["scale"])), max(1, int(rh * settings["scale"]))), interpolation=cv2.INTER_AREA)
                result = self.recognize(image)
                finished = self.clock()
                passes.append((started, finished))
                elapsed = max(finished - passes[0][0], 0.001)
                result = {**result, "elapsed_ms": round((finished - started) * 1000),
                          "ocr_latency_ms": round((finished - started) * 1000),
                          "passes_per_second": round(len(passes) / elapsed, 2),
                          "timestamp_ms": round(time.time() * 1000), "session_id": session}
                with self.lock:
                    if (self.stop_event.is_set() or revision != self.revision
                            or self.camera.snapshot()["session_id"] != session):
                        continue
                    self._state(True)
                    self.emit("live_recognition", result)
                    if detector.observe(result, started, settings["confirm_passes"], settings["dedup_gap"]):
                        capture_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:12]
                        saved = self.save(frame, capture_id, recognize=lambda _: result)
                        self.emit("ticket_detected", saved)
            except BrokenPipeError:
                return
            except Exception:
                # Neither private OCR text nor library exception details go to logs.
                self._state(False, "ライブ認識に失敗しました。カメラ・保存先・認識モデルを確認してください。")
                self.stop_event.wait(0.5)
