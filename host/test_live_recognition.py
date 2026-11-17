"""Headless live OCR, timing and native-control contracts; no real camera/model."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

from desktop_backend import BackendService, validate_request
from live_recognition import DEFAULTS, LiveRecognition, TicketDetector
from recognition import capture_and_recognize
from test_desktop_backend import RecordingOutbox
from test_usb_camera import FakeCapture, FakeControls, wait_for
from camera_stream import CameraStream, empty_camera


def match(name="カレー", score=0.8, exact=False):
    return {"status": "matched", "name": name, "price": 350, "score": score,
            "exact_name": exact, "text": name, "confidence": 90, "message": "一致"}


MISS = {"status": "no_match", "name": None, "price": None, "score": None, "text": ""}


class DetectorTests(unittest.TestCase):
    def test_adaptive_confirmation_and_interruption(self):
        d = TicketDetector()
        self.assertFalse(d.observe(match(), 0))
        self.assertFalse(d.observe(MISS, 0.1))
        self.assertFalse(d.observe(match(), 0.2))
        self.assertTrue(d.observe(match(), 0.3))
        self.assertFalse(d.observe(match(), 20), "Same ticket is suppressed without observed absence")
        self.assertTrue(TicketDetector().observe(match(exact=True), 0))
        self.assertTrue(TicketDetector().observe(match(score=0.97), 0))
        d = TicketDetector()
        self.assertFalse(d.observe(match(exact=True), 0, confirm_passes=2))
        self.assertTrue(d.observe(match(exact=True), 0.1, confirm_passes=2))
        self.assertFalse(TicketDetector().observe(match(), 0))
        d = TicketDetector()
        d.observe(match(), 0)
        self.assertFalse(d.observe(match(), 2), "Old confirmations expire")

    def test_each_item_requires_full_observed_absence_gap(self):
        d = TicketDetector()
        a, b = match(exact=True), match("うどん", exact=True)
        self.assertTrue(d.observe(a, 0))
        self.assertTrue(d.observe(b, 0.1))
        self.assertFalse(d.observe(a, 1.0))
        self.assertFalse(d.observe(b, 1.1))
        self.assertFalse(d.observe(MISS, 1.2))
        self.assertFalse(d.observe(MISS, 2.49))
        self.assertTrue(d.observe(a, 2.61))
        self.assertFalse(d.observe(b, 2.62), "B was last present later than A")
        d.observe(MISS, 2.7)
        self.assertFalse(d.observe(b, 2.71), "Returning B resets its own absence clock")
        d.observe(MISS, 2.8)
        self.assertTrue(d.observe(b, 4.31))
        d = TicketDetector()
        self.assertTrue(d.observe(a, 0, dedup_gap=0.2))
        d.observe(MISS, 0.1, dedup_gap=0.2)
        self.assertTrue(d.observe(a, 0.31, dedup_gap=0.2))


class LatestCamera:
    def __init__(self):
        self.frame = None
        self.sequence = 0
        self.session = "test-session"
        self.lock = threading.Lock()

    def push(self, value):
        with self.lock:
            self.sequence += 1
            self.frame = np.full((40, 80, 3), value, dtype=np.uint8)

    def snapshot(self):
        return {**empty_camera(), "connected": self.frame is not None, "session_id": self.session}

    def latest_frame(self, *, after=None, with_metadata=False):
        with self.lock:
            if self.frame is None or after == self.sequence:
                return None
            return self.frame.copy(), self.sequence, self.session

    def stop(self):
        pass


class WorkerTests(unittest.TestCase):
    def make_worker(self, recognize, save=None):
        camera = LatestCamera()
        events = []
        worker = LiveRecognition(camera, lambda event, payload: events.append((event, payload)),
            recognize=recognize, save=save or Mock(side_effect=lambda frame, cid, recognize: {**recognize(None), "capture_id": cid}),
            readiness=lambda: {"ready": True, "error": None})
        self.addCleanup(worker.close)
        return camera, events, worker

    def test_only_newest_frame_after_busy_and_no_repeat_of_frozen_frame(self):
        entered, release = threading.Event(), threading.Event()
        seen = []
        def recognize(frame):
            seen.append(int(frame[0, 0, 0]))
            if len(seen) == 1:
                entered.set()
                release.wait(2)
            return dict(MISS)
        camera, events, worker = self.make_worker(recognize)
        self.addCleanup(release.set)
        camera.push(1)
        worker.start()
        self.assertTrue(entered.wait(1))
        for value in [2, 3, 4]:
            camera.push(value)
        self.assertEqual(seen, [1])
        release.set()
        wait_for(lambda: len([e for e in events if e[0] == "live_recognition"]) == 2)
        threading.Event().wait(0.08)
        self.assertEqual(seen, [1, 4])
        result = [e[1] for e in events if e[0] == "live_recognition"][-1]
        self.assertEqual(result["status"], "no_match")
        self.assertGreater(result["passes_per_second"], 0)
        self.assertGreaterEqual(result["ocr_latency_ms"], 0)

    def test_pause_discards_inflight_result_then_resume_and_full_frame_save(self):
        entered, release = threading.Event(), threading.Event()
        shapes, saved = [], []
        def recognize(frame):
            shapes.append(frame.shape)
            entered.set(); release.wait(2)
            return match(exact=True)
        def save(frame, cid, recognize):
            saved.append((frame.shape, cid))
            return {**recognize(None), "capture_id": cid}
        camera, events, worker = self.make_worker(recognize, save)
        camera.push(1)
        worker.configure({"roi": 0.5, "scale": 0.5})
        worker.start()
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(1))
        worker.configure({"enabled": False})
        release.set()
        threading.Event().wait(0.08)
        self.assertFalse(any(e[0] in {"ticket_detected", "live_recognition"} for e in events))
        camera.push(2)
        worker.configure({"enabled": True})
        wait_for(lambda: bool(saved))
        self.assertEqual(shapes, [(10, 20, 3), (10, 20, 3)])
        self.assertEqual(saved[0][0], (40, 80, 3))
        self.assertRegex(saved[0][1], r"^\d{8}_\d{6}_[a-f0-9]{12}$")
        camera.push(3)
        wait_for(lambda: len(shapes) >= 3)
        self.assertEqual(len(saved), 1)

    def test_backend_auto_start_and_validated_tuning(self):
        camera = LatestCamera()
        outbox = RecordingOutbox()
        worker = LiveRecognition(camera, outbox.emit, recognize=Mock(return_value=dict(MISS)),
            readiness=lambda: {"ready": True, "error": None})
        service = BackendService(outbox, camera=camera, devices=lambda: [],
            readiness=lambda: {"ready": True, "error": None}, live=worker)
        self.addCleanup(service.close)
        self.assertTrue(worker.snapshot()["enabled"])
        camera.push(1)
        wait_for(lambda: any(row.get("event") == "live_recognition" for row in outbox.messages))
        service.handle({"id": 1, "action": "set_live", "enabled": False, "confirm_passes": 3,
                        "dedup_gap": 2, "roi": 0.75, "scale": 0.5})
        reply = next(row for row in outbox.messages if row.get("id") == 1)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["result"]["confirm_passes"], 3)
        self.assertFalse(reply["result"]["enabled"])
        before = worker.snapshot()
        service.handle({"id": 2, "action": "set_live", "enabled": True, "roi": 0})
        self.assertFalse(outbox.messages[-1]["ok"])
        self.assertEqual(worker.snapshot(), before)

    def test_old_session_inflight_pass_is_discarded(self):
        entered, release = threading.Event(), threading.Event()
        def recognize(frame):
            entered.set(); release.wait(2)
            return match(exact=True)
        camera, events, worker = self.make_worker(recognize)
        self.addCleanup(release.set)
        camera.push(1); worker.start()
        self.assertTrue(entered.wait(1))
        camera.session = "new-session"
        release.set()
        threading.Event().wait(0.08)
        self.assertFalse(any(e[0] in {"ticket_detected", "live_recognition"} for e in events))
        camera.push(2)
        wait_for(lambda: any(e[0] == "ticket_detected" for e in events))
        self.assertEqual([e[1]["session_id"] for e in events if e[0] == "live_recognition"], ["new-session"])

    def test_live_validation_rejects_types_bounds_unknowns(self):
        for change in [{}, {"enabled": 1}, {"confirm_passes": True}, {"confirm_passes": 1.5},
                       {"confirm_passes": -1}, {"confirm_passes": 11}, {"dedup_gap": 0},
                       {"dedup_gap": 31}, {"roi": float("nan")}, {"scale": float("inf")},
                       {"scale": 1.1}, {"roi": "1"}, {"path": "private"}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_request({"id": 1, "action": "set_live", **change})
        for key, value in DEFAULTS.items():
            validate_request({"id": 1, "action": "set_live", key: value})

    def test_capture_conventions_full_resolution_json_and_no_overwrite(self):
        root = Path("/native/local/tmp/ctocr-realtime/capture-tests")
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            target = Path(directory)
            frame = np.full((40, 80, 3), 80, np.uint8)
            outcome = {**match(exact=True), "ocr_latency_ms": 21, "passes_per_second": 12.5}
            result = capture_and_recognize(frame, "20261117_120000_abcdef123456", target, lambda _: outcome)
            self.assertEqual(cv2.imread(str(target / "20261117_120000_abcdef123456_raw.png")).shape, frame.shape)
            self.assertEqual(json.loads(next(target.glob("*_result.json")).read_text()), result)
            before = {p.name: p.read_bytes() for p in target.iterdir()}
            with self.assertRaises(FileExistsError):
                capture_and_recognize(frame, result["capture_id"], target)
            self.assertEqual(before, {p.name: p.read_bytes() for p in target.iterdir()})


class ImmediateControlTests(unittest.TestCase):
    def test_slow_control_write_does_not_block_capture(self):
        entered, release = threading.Event(), threading.Event()
        capture, controls = FakeCapture(), FakeControls()
        original = controls.apply
        def apply(request):
            if request.gain == "30":
                entered.set(); release.wait(2)
            return original(request)
        controls.apply = apply
        stream = CameraStream(lambda *_: None, capture_factory=lambda *_: capture,
            controls_factory=lambda _: controls, devices=lambda: [{"id": "/dev/video4"}], owner_check=lambda _: False)
        try:
            stream.start(); wait_for(lambda: stream.snapshot()["connected"])
            stream.set_controls({"gain": 30})
            self.assertTrue(entered.wait(1))
            before = capture.reads
            wait_for(lambda: capture.reads >= before + 3, timeout=0.5)
        finally:
            release.set(); stream.stop()

    def test_control_application_continues_while_capture_read_is_blocked(self):
        release = threading.Event()
        capture, controls = FakeCapture(frozen=release), FakeControls()
        stream = CameraStream(lambda *_: None, capture_factory=lambda *_: capture,
            controls_factory=lambda _: controls, devices=lambda: [{"id": "/dev/video4"}], owner_check=lambda _: False)
        try:
            stream.start()
            wait_for(lambda: capture.reads >= 2 and stream.snapshot()["connected"])
            stream.set_controls({"gain": 30})
            wait_for(lambda: stream.snapshot()["gain"] == 30, timeout=0.7)
            self.assertEqual(capture.reads, 2, "Settings do not wait for a capture read")
            self.assertEqual(stream.snapshot()["controls"]["gain"]["step"], 2)
        finally:
            release.set(); stream.stop()

    def test_raw_control_bounds_menu_holes_and_flags(self):
        controls = FakeControls()
        controls.apply_values({"gain": 12})
        self.assertEqual(controls.values["gain"], 12)
        for values in [{"gain": 13}, {"auto_exposure": 2}, {"exposure_time_absolute": 4}, {"unknown": 1}]:
            before = list(controls.writes)
            with self.assertRaises(ValueError):
                controls.apply_values(values)
            self.assertEqual(controls.writes, before)
        for values in [{"gain;exec": 1}, {"gain": True}, {"gain": 1.5}, {}, {"gain": "1"}]:
            with self.assertRaises(ValueError):
                validate_request({"id": 1, "action": "set_camera", "controls": values})
        validate_request({"id": 1, "action": "set_camera", "controls": {"brightness": -10}})


if __name__ == "__main__":
    unittest.main()
