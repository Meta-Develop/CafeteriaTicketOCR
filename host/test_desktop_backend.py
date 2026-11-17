"""Focused protocol, lifecycle and preserved-result checks; no hardware/network."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

import app
import desktop_backend as backend
import recognition
from camera_stream import empty_camera
from test_usb_camera import wait_for

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".agents/temp/tauri-rebuild/backend-tests"


class RecordingOutbox:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)

    def emit(self, name, payload):
        self.messages.append({"event": name, "payload": payload})


class FakeCamera:
    def __init__(self, frame=None):
        self.frame = frame
        self.started = []
        self.stopped = 0
        self.settings = []

    def snapshot(self):
        return {**empty_camera(), "connected": self.frame is not None,
                "session_id": "fake-session" if self.frame is not None else None}

    def latest_frame(self):
        return self.frame.copy() if self.frame is not None else None

    def start(self, device):
        self.started.append(device)

    def stop(self):
        self.stopped += 1

    def set_controls(self, settings):
        self.settings.append(settings)
        return {"accepted": True}


class ProtocolTests(unittest.TestCase):
    def make_service(self, *, frame=None, recognize=None, readiness=None):
        self.outbox = RecordingOutbox()
        self.camera = FakeCamera(frame)
        return backend.BackendService(self.outbox, camera=self.camera,
            devices=lambda: [{"id": "/dev/video4", "label": "sample"}],
            readiness=readiness or (lambda: {"ready": True, "menu_count": 2, "error": None}),
            recognize=recognize or Mock())

    def test_status_start_set_stop_shutdown_and_release(self):
        service = self.make_service()
        for request in (
            {"id": 1, "action": "status"},
            {"id": 2, "action": "start_camera", "device": "/dev/video4"},
            {"id": 3, "action": "set_camera", "exposure_mode": "manual", "exposure_us": 100, "gain": 24},
            {"id": 4, "action": "stop_camera"},
            {"id": 5, "action": "shutdown"},
        ):
            service.handle(request)
        self.assertTrue(all(message["ok"] for message in self.outbox.messages))
        self.assertEqual(self.camera.started, ["/dev/video4"])
        self.assertEqual(self.camera.settings, [{"exposure_mode": "manual", "exposure_us": 100, "gain": 24}])
        service.close()
        self.assertEqual(self.camera.stopped, 2)
        self.assertTrue(service.closing)

    def test_no_frame_unavailable_busy_and_reply_before_actual_result(self):
        release = threading.Event()
        def recognize(frame, capture_id):
            self.assertEqual(frame.shape, (720, 1280, 3))
            release.wait(2)
            return {"capture_id": capture_id, "status": "no_match", "name": None, "text": ""}
        service = self.make_service(recognize=recognize)
        service.handle({"id": 1, "action": "recognize"})
        self.assertIn("映像", self.outbox.messages[-1]["error"])
        self.camera.frame = np.zeros((720, 1280, 3), np.uint8)
        service.handle({"id": 2, "action": "recognize"})
        self.assertTrue(self.outbox.messages[-1]["result"]["accepted"])
        capture_id = self.outbox.messages[-1]["result"]["capture_id"]
        service.handle({"id": 3, "action": "recognize"})
        self.assertIn("認識中", self.outbox.messages[-1]["error"])
        service.handle({"id": 4, "action": "status"})
        self.assertTrue(self.outbox.messages[-1]["result"]["busy"])
        release.set()
        service.job.join(2)
        result = self.outbox.messages[-1]
        self.assertEqual(result["event"], "recognition_result")
        self.assertEqual(result["payload"]["capture_id"], capture_id)
        self.assertFalse(service.busy)
        not_ready = self.make_service(frame=self.camera.frame, readiness=lambda: {"ready": False, "menu_count": 0, "error": "モデルがありません"})
        not_ready.handle({"id": 5, "action": "recognize"})
        self.assertIn("モデル", self.outbox.messages[-1]["error"])

    def test_fast_result_still_follows_its_acceptance_and_errors_do_not_leak(self):
        def fail(*_):
            raise RuntimeError("PRIVATE_MENU_TEXT must never appear in logs")
        service = self.make_service(frame=np.zeros((4, 4, 3), np.uint8), recognize=fail)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            service.handle({"id": 1, "action": "recognize"})
            service.job.join(2)
        self.assertTrue(self.outbox.messages[0]["result"]["accepted"])
        self.assertEqual(self.outbox.messages[1]["event"], "recognition_result")
        self.assertEqual(self.outbox.messages[1]["payload"]["status"], "error")
        self.assertNotIn("PRIVATE", json.dumps(self.outbox.messages) + stderr.getvalue())
        self.assertFalse(service.busy)

    def test_untrusted_fields_actions_types_nan_and_paths_are_rejected(self):
        service = self.make_service()
        invalid = [[], None, {"id": True, "action": "status"}, {"id": 1, "action": []},
            {"id": 2, "action": "exec", "command": "PRIVATE"},
            {"id": 3, "action": "status", "path": "PRIVATE"},
            {"id": 4, "action": "set_camera", "gain": float("nan")},
            {"id": 5, "action": "set_camera", "gain": True},
            {"id": 6, "action": "set_camera", "exposure_mode": "manual"},
            {"id": 7, "action": "set_camera", "exposure_mode": {}},
            {"id": 8, "action": "start_camera", "device": []},
            {"id": 9, "action": "set_camera"}]
        for request in invalid:
            service.handle(request)
            self.assertFalse(self.outbox.messages[-1]["ok"], request)
            self.assertNotIn("PRIVATE", str(self.outbox.messages[-1]))
        self.assertFalse(self.camera.started)
        self.assertFalse(self.camera.settings)

    def test_gain_can_follow_negative_hardware_ranges(self):
        service = self.make_service()
        service.handle({"id": 1, "action": "set_camera", "gain": -2})
        self.assertTrue(self.outbox.messages[-1]["ok"])
        self.assertEqual(self.camera.settings, [{"gain": -2}])

    def test_real_process_protocol_is_only_json_and_eof_exits_cleanly(self):
        requests = ["bad PRIVATE JSON\n", "[1]\n", "x" * 5000 + "\n",
            json.dumps({"id": 1, "action": "status"}) + "\n",
            json.dumps({"id": 2, "action": "recognize"}) + "\n",
            json.dumps({"id": 3, "action": "shutdown"}) + "\n"]
        result = subprocess.run([sys.executable, "-B", "host/desktop_backend.py"],
            input="".join(requests), cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        replies = [row for row in rows if "id" in row]
        self.assertEqual(len(replies), 6)
        self.assertEqual([row["id"] for row in replies[-3:]], [1, 2, 3])
        self.assertFalse(replies[-2]["ok"])
        self.assertEqual(replies[-3]["result"]["camera"]["connected"], False)
        self.assertIsNone(replies[-3]["result"]["camera"]["session_id"])
        self.assertNotIn("PRIVATE", result.stdout + result.stderr)
        eof = subprocess.run([sys.executable, "-B", "host/desktop_backend.py"],
            input="", cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(eof.returncode, 0)

    def test_outbox_backpressure_keeps_one_latest_preview_and_ordered_replies(self):
        entered, release = threading.Event(), threading.Event()
        class SlowStream(io.StringIO):
            def write(self, text):
                entered.set()
                release.wait(2)
                return super().write(text)
        sink = SlowStream()
        outbox = backend.Outbox(sink)
        outbox.send({"id": 1, "ok": True, "result": {}})
        self.assertTrue(entered.wait(1))
        for index in range(1000):
            outbox.emit("camera_frame", {"timestamp_ms": index})
        with outbox.condition:
            self.assertEqual(len(outbox.latest), 1)
            self.assertEqual(outbox.latest["camera_frame"]["payload"]["timestamp_ms"], 999)
        outbox.send({"id": 2, "ok": True, "result": {}})
        release.set()
        wait_for(lambda: '999' in sink.getvalue())
        outbox.close()
        rows = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in rows if "id" in r], [1, 2])
        self.assertEqual(len([r for r in rows if r.get("event") == "camera_frame"]), 1)


    def test_disconnect_discards_the_pending_preview(self):
        entered, release = threading.Event(), threading.Event()
        class SlowStream(io.StringIO):
            def write(self, text):
                entered.set()
                release.wait(2)
                return super().write(text)
        sink = SlowStream()
        outbox = backend.Outbox(sink)
        outbox.send({"id": 1, "ok": True, "result": {}})
        self.assertTrue(entered.wait(1))
        outbox.emit("camera_frame", {"jpeg": "old frame"})
        outbox.emit("camera_state", empty_camera())
        with outbox.condition:
            self.assertNotIn("camera_frame", outbox.latest)
        release.set()
        outbox.close()
        self.assertNotIn("old frame", sink.getvalue())


class RecognitionTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.directory = Path(self.temp.name)
        self.menu = self.directory / "menu.json"
        self.menu.write_text(json.dumps([{"name": "カレー", "price": 350, "type": "other"}]), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_match_and_no_match_preserve_existing_selector(self):
        for text, expected in (("カレー 350円", "matched"), ("未知の文章", "no_match"), ("", "no_match")):
            engine = Mock(return_value=SimpleNamespace(txts=[text], scores=[0.9]))
            result = recognition.recognize_image(self.directory / "image.png", engine, self.menu)
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["name"], "カレー" if expected == "matched" else None)
            self.assertEqual(result["price"], 350 if expected == "matched" else None)
            self.assertEqual(result["text"], text)

    def test_png_full_resolution_result_and_errors_preserved_without_log_text(self):
        frame = np.zeros((720, 1280, 3), np.uint8)
        outcome = {"status": "no_match", "name": None, "price": None, "score": None,
                   "text": "PRIVATE OCR TEXT", "confidence": 85, "message": "一致なし"}
        result = recognition.capture_and_recognize(frame, "sample", self.directory, lambda _: outcome)
        import cv2
        self.assertEqual(cv2.imread(str(self.directory / "sample_raw.png")).shape, frame.shape)
        stored = json.loads((self.directory / "sample_result.json").read_text())
        self.assertEqual(stored, result)
        self.assertEqual(stored["text"], outcome["text"])
        self.assertEqual((self.directory / "sample_ocr_result.txt").read_text(), "")
        def fail(_):
            raise ValueError("PRIVATE OCR TEXT")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            error = recognition.capture_and_recognize(frame, "failed", self.directory, fail)
        self.assertEqual(error["status"], "error")
        self.assertTrue((self.directory / "failed_raw.png").exists())
        self.assertEqual(json.loads((self.directory / "failed_result.json").read_text())["status"], "error")
        self.assertNotIn("PRIVATE", stderr.getvalue())

    def test_missing_models_cannot_download_and_compatibility_launcher_is_small(self):
        with patch.object(recognition, "_RAPIDOCR_ENGINE", None), patch.object(recognition, "rapidocr_model_paths", return_value={"Rec": self.directory / "missing.onnx"}):
            with self.assertRaisesRegex(RuntimeError, "モデル"):
                recognition.get_rapidocr_engine(allow_download=False)
        self.assertIs(app.get_rapidocr_engine, recognition.get_rapidocr_engine)
        with patch.object(app.os, "execv") as execv, patch.object(sys, "argv", ["app.py", "--check"]):
            app.main()
        self.assertEqual(execv.call_args.args[1][-2:], [str(ROOT / "scripts/run_app.sh"), "--check"])


if __name__ == "__main__":
    unittest.main()
