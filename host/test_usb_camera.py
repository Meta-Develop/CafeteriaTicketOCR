"""Native USB controls and asynchronous capture tests; no physical device is opened."""
from __future__ import annotations

import base64
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

import camera_stream
from camera_stream import CameraStream
import usb_camera
from usb_camera import UsbControls, V4L2Controls, discover_usb_sources, parse_controls, parse_usb_source


class FakeControls(V4L2Controls):
    def __init__(self, reject=None, clamp=None):
        super().__init__(4)
        self.values = {"auto_exposure": 3, "exposure_time_absolute": 157, "gain": 0, "exposure_dynamic_framerate": 1}
        self.writes = []
        self.reject = reject
        self.clamp = clamp

    def _run(self, option):
        if option == "--list-ctrls-menus":
            inactive = "inactive," if self.values["auto_exposure"] != 1 else ""
            return f"""
 auto_exposure 0x009a0901 (menu) : min=0 max=3 default=3 value={self.values['auto_exposure']}
     1: Manual Mode
     3: Aperture Priority Mode
 exposure_time_absolute 0x009a0902 (int) : min=1 max=5000 step=1 default=157 value={self.values['exposure_time_absolute']} flags={inactive}has-min-max
 gain 0x00980913 (int) : min=0 max=64 step=2 default=0 value={self.values['gain']}
 exposure_dynamic_framerate 0x009a0903 (bool) : default=0 value={self.values['exposure_dynamic_framerate']}
"""
        name, value = option.removeprefix("--set-ctrl=").split("=")
        self.writes.append((name, int(value)))
        if name == self.reject:
            raise RuntimeError("driver rejected control")
        self.values[name] = 2 if name == self.clamp else int(value)
        return ""


class ControlTests(unittest.TestCase):
    def test_parse_ranges_flags_and_nonboolean_auto_modes(self):
        controls = FakeControls().query()
        exposure = controls["exposure_time_absolute"]
        self.assertEqual((exposure.minimum, exposure.maximum, exposure.step, exposure.value), (1, 5000, 1, 157))
        self.assertIn("inactive", exposure.flags)
        self.assertEqual(controls["auto_exposure"].choices, {1: "Manual Mode", 3: "Aperture Priority Mode"})
        self.assertEqual(controls["exposure_dynamic_framerate"].maximum, 1)
        self.assertEqual(parse_controls("not a control"), {})

    def test_minimum_manual_exposure_and_readback(self):
        controls = FakeControls()
        report = controls.apply(UsbControls())
        self.assertEqual(controls.writes, [("auto_exposure", 1), ("exposure_time_absolute", 1), ("gain", 0), ("exposure_dynamic_framerate", 0)])
        self.assertIn("exposure_time_absolute=1 (0.1 ms)", report)
        self.assertIn("[1..5000; step 1]", report)
        self.assertNotIn("ERROR", report)

    def test_auto_does_not_write_manual_exposure(self):
        controls = FakeControls()
        controls.apply(UsbControls(auto=True, exposure="invalid", gain=""))
        self.assertEqual(controls.writes, [("auto_exposure", 3), ("exposure_dynamic_framerate", 0)])

    def test_bounds_and_step_rejected_without_driver_write(self):
        controls = FakeControls()
        report = controls.apply(UsbControls(exposure="5001", gain="3"))
        self.assertIn("expected 1..5000", report)
        self.assertIn("expected 0..64, step 2", report)
        self.assertFalse(any(name in {"gain", "exposure_time_absolute"} for name, _ in controls.writes))

    def test_rejected_auto_skips_exposure(self):
        controls = FakeControls(reject="auto_exposure")
        report = controls.apply(UsbControls())
        self.assertIn("driver rejected control", report)
        self.assertIn("manual mode not confirmed", report)
        self.assertNotIn(("exposure_time_absolute", 1), controls.writes)

    def test_driver_clamp_shown_as_readback_mismatch(self):
        report = FakeControls(clamp="exposure_time_absolute").apply(UsbControls())
        self.assertIn("requested 1, readback 2", report)
        self.assertIn("exposure_time_absolute=2 (0.2 ms)", report)

    def test_missing_controls_reported_without_guesses(self):
        controls = FakeControls()
        controls.query = lambda: {}
        report = controls.apply(UsbControls())
        self.assertIn("unsupported", report)
        self.assertEqual(controls.writes, [])

    def test_mode_disappearing_on_readback_is_reported(self):
        controls = FakeControls()
        original_query = controls.query
        count = [0]
        def query():
            count[0] += 1
            values = original_query()
            if count[0] > 1:
                values.pop("auto_exposure")
            return values
        controls.query = query
        report = controls.apply(UsbControls())
        self.assertIn("manual mode not confirmed", report)
        self.assertIn("readback missing", report)

    def test_command_failure_timeout_and_missing_tool(self):
        controls = V4L2Controls(4)
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("v4l2-ctl", 2)):
            with self.subTest(error=type(error).__name__), patch.object(usb_camera.subprocess, "run", side_effect=error):
                with self.assertRaisesRegex(RuntimeError, "unavailable"):
                    controls.query()
        with patch.object(usb_camera.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "device busy")) as run:
            with self.assertRaisesRegex(RuntimeError, "device busy"):
                controls.query()
            self.assertEqual(run.call_args.args[0], ["v4l2-ctl", "--device=/dev/video4", "--list-ctrls-menus"])
            self.assertEqual(run.call_args.kwargs["timeout"], 2)


class FakeCapture:
    def __init__(self, *, opened=True, disconnect=False, frozen=None):
        self.opened = opened
        self.disconnect = disconnect
        self.frozen = frozen
        self.calls = []
        self.released = threading.Event()
        self.reads = 0
        self.image = np.full((720, 1280, 3), 80, dtype=np.uint8)

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.calls.append((prop, value))
        return True

    def read(self):
        self.reads += 1
        if self.frozen and self.reads > 1:
            self.frozen.wait(3)
        else:
            threading.Event().wait(0.006)
        if self.disconnect and self.reads > 1:
            return False, None
        return True, self.image

    def release(self):
        self.released.set()


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.01)
    raise AssertionError("condition not met within bounded wait")


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.streams = []

    def tearDown(self):
        for stream in self.streams:
            stream.stop()

    def make_stream(self, factory, **kwargs):
        stream = CameraStream(lambda name, payload: self.events.append((name, payload)),
            capture_factory=factory, controls_factory=lambda _: FakeControls(),
            devices=lambda: [{"id": "/dev/video4", "label": "sample"}], owner_check=lambda _: False,
            **kwargs)
        self.streams.append(stream)
        return stream

    def test_capture_runs_without_preview_throttle_and_keeps_full_resolution(self):
        capture = FakeCapture()
        opened = Mock(return_value=capture)
        stream = self.make_stream(opened)
        self.assertEqual(opened.call_count, 0)
        stream.start()
        wait_for(lambda: len([e for e in self.events if e[0] == "camera_frame"]) >= 3)
        self.assertEqual(stream.latest_frame().shape, (720, 1280, 3))
        jpeg = [e[1] for e in self.events if e[0] == "camera_frame"][-1]
        image = cv2.imdecode(np.frombuffer(base64.b64decode(jpeg["jpeg"]), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(image.shape, (360, 640, 3))
        self.assertGreater(capture.reads, len([e for e in self.events if e[0] == "camera_frame"]))
        self.assertEqual(stream.snapshot()["brightness"], 80)
        self.assertIn((cv2.CAP_PROP_BUFFERSIZE, 2), capture.calls)
        self.assertIn((cv2.CAP_PROP_FPS, 120), capture.calls)
        self.assertEqual(stream.snapshot()["exposure_us"], 100)
        self.assertEqual(stream.snapshot()["gain"], 24)
        self.assertTrue(stream.stop())
        self.assertTrue(capture.released.is_set())
        self.assertIsNone(stream.latest_frame())

    def test_pending_controls_are_latest_only_and_native_values_clamp(self):
        stream = self.make_stream(Mock())
        controls = FakeControls()
        stream._apply(controls, {"exposure_mode": "manual", "exposure_us": 777, "gain": 999})
        self.assertEqual(controls.values["exposure_time_absolute"], 8)
        self.assertEqual(stream.snapshot()["exposure_us"], 800)
        self.assertEqual(stream.snapshot()["gain"], 64)
        self.assertEqual(stream.snapshot()["gain_min"], 0)
        self.assertEqual(stream.snapshot()["gain_max"], 64)
        self.assertFalse(stream.snapshot()["auto_exposure"])
        stream._apply(controls, {"exposure_mode": "auto", "gain": 24})
        self.assertTrue(stream.snapshot()["auto_exposure"])
        self.assertEqual(stream.snapshot()["exposure_us"], 800)
        stream.thread = Mock(is_alive=lambda: True)
        stream.set_controls({"gain": 2})
        stream.set_controls({"gain": 4})
        self.assertEqual(stream.pending["gain"], 4)
        stream.thread = None

    def test_unsupported_or_failed_controls_never_invent_readback(self):
        stream = self.make_stream(Mock())
        controls = Mock()
        controls.query.return_value = {}
        controls.apply.return_value = "ERROR: unsupported"
        stream._apply(controls, {"exposure_mode": "short", "gain": 24})
        self.assertIsNone(stream.snapshot()["exposure_us"])
        self.assertIsNone(stream.snapshot()["gain"])
        self.assertIsNotNone(stream.snapshot()["error"])
        controls.query.side_effect = RuntimeError("private exception details")
        stream._apply(controls, {"exposure_mode": "short", "gain": 24})
        self.assertNotIn("private", str(self.events))

    def test_open_failure_releases_then_reconnects(self):
        failed, restored = FakeCapture(opened=False), FakeCapture()
        opened = Mock(side_effect=[failed, restored])
        stream = self.make_stream(opened)
        stream.start()
        wait_for(failed.released.is_set)
        wait_for(lambda: stream.snapshot()["connected"])
        self.assertTrue(failed.released.is_set())
        self.assertEqual(opened.call_count, 2)
        stream.stop()
        self.assertTrue(restored.released.is_set())

    def test_disconnect_clears_stale_frame_before_reconnect(self):
        gone, restored = FakeCapture(disconnect=True), FakeCapture()
        stream = self.make_stream(Mock(side_effect=[gone, restored]))
        stream.start()
        wait_for(gone.released.is_set)
        self.assertFalse(stream.snapshot()["connected"])
        self.assertIsNone(stream.snapshot()["session_id"])
        self.assertIsNone(stream.latest_frame())
        wait_for(lambda: stream.snapshot()["connected"])
        sessions = [payload["session_id"] for name, payload in self.events
                    if name == "camera_state" and payload["connected"]]
        self.assertEqual(len(set(sessions)), 2, "reopening the same device must create a fresh session")
        connected = None
        seen_frames = set()
        for name, payload in self.events:
            if name == "camera_state":
                connected = payload["session_id"]
                self.assertEqual(payload["connected"], connected is not None)
            elif name == "camera_frame":
                self.assertIsInstance(payload["session_id"], str)
                self.assertTrue(payload["session_id"])
                self.assertEqual(payload["session_id"], connected, "state must precede its first frame")
                seen_frames.add(connected)
        self.assertEqual(seen_frames, set(sessions), "the first preview of each session must work")
        stream.stop()
        self.assertIsNone(stream.snapshot()["session_id"])
        self.assertTrue(restored.released.is_set())

    def test_stop_rejects_an_encoded_old_frame_and_reopen_gets_a_new_session(self):
        entered, release = threading.Event(), threading.Event()
        captures = [FakeCapture(), FakeCapture()]
        stream = self.make_stream(Mock(side_effect=captures))
        encode = cv2.imencode
        calls = 0

        def delayed_encode(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                release.wait(3)
            return encode(*args)

        with patch.object(camera_stream.cv2, "imencode", side_effect=delayed_encode):
            stream.start()
            try:
                self.assertTrue(entered.wait(2))
                old_session = stream.snapshot()["session_id"]
                self.assertIsInstance(old_session, str)
                self.assertFalse(stream.stop())
                boundary = len(self.events)
                self.assertFalse(stream.snapshot()["connected"])
                self.assertIsNone(stream.snapshot()["session_id"])
                self.assertIsNone(stream.latest_frame())
            finally:
                release.set()
            wait_for(captures[0].released.is_set)
            wait_for(lambda: not stream.thread.is_alive())
            stream.start("/dev/video4")
            wait_for(lambda: any(name == "camera_frame" for name, _ in self.events[boundary:]))
            new_session = stream.snapshot()["session_id"]
            self.assertNotEqual(new_session, old_session)
            frames = [payload for name, payload in self.events[boundary:] if name == "camera_frame"]
            self.assertTrue(frames)
            self.assertTrue(all(payload["session_id"] == new_session for payload in frames))

    def test_retired_monitor_cannot_disconnect_the_new_session(self):
        entered, release = threading.Event(), threading.Event()

        class PausedWait(threading.Event):
            def wait(self, timeout=None):
                entered.set()
                release.wait(2)
                # Model the stop occurring immediately after wait returned False.
                return False

        stream = self.make_stream(Mock())
        retired = stream.stop_event = PausedWait()
        monitor = threading.Thread(target=stream._watch, args=(retired, threading.Event()), daemon=True)
        monitor.start()
        try:
            self.assertTrue(entered.wait(1))
            with stream.lock:
                retired.set()
                stream.stop_event = threading.Event()
                stream.session_id = "new-session"
                stream.state.update(connected=True, session_id="new-session")
                stream.frame_time = time.monotonic() - 10
        finally:
            release.set()
            monitor.join(1)
        self.assertFalse(monitor.is_alive())
        self.assertEqual(self.events, [])
        self.assertTrue(stream.snapshot()["connected"])
        self.assertEqual(stream.snapshot()["session_id"], "new-session")

    def test_freeze_detected_and_blocked_read_prevents_second_owner(self):
        unblock = threading.Event()
        capture = FakeCapture(frozen=unblock)
        opened = Mock(return_value=capture)
        stream = self.make_stream(opened, freeze_seconds=0.04)
        stream.start()
        wait_for(lambda: "映像が止まり" in (stream.snapshot()["error"] or ""))
        self.assertIsNone(stream.latest_frame())
        self.assertFalse(stream.stop())
        with self.assertRaisesRegex(ValueError, "解放中"):
            stream.start()
        self.assertEqual(opened.call_count, 1)
        unblock.set()
        wait_for(capture.released.is_set)

    def test_initial_read_freeze_is_reported_without_a_first_frame(self):
        unblock = threading.Event()
        capture = FakeCapture()
        def blocked_read():
            unblock.wait(3)
            return False, None
        capture.read = blocked_read
        stream = self.make_stream(Mock(return_value=capture), freeze_seconds=0.04)
        stream.start()
        try:
            wait_for(lambda: "映像が止まり" in (stream.snapshot()["error"] or ""))
            self.assertFalse(stream.snapshot()["connected"])
            self.assertIsNone(stream.latest_frame())
        finally:
            unblock.set()
        stream.stop()
        self.assertTrue(capture.released.is_set())

    def test_periodic_auto_exposure_readback_is_actual_and_preview_is_bounded(self):
        controls = FakeControls()
        capture = FakeCapture()
        stream = self.make_stream(Mock(return_value=capture))
        stream.controls_factory = lambda _: controls
        stream.settings = {"exposure_mode": "auto", "gain": 24}
        stream.start()
        wait_for(lambda: stream.snapshot()["connected"])
        controls.values["exposure_time_absolute"] = 80
        wait_for(lambda: stream.snapshot()["exposure_us"] == 8000)
        stream.stop()
        timestamps = [event[1]["timestamp_ms"] for event in self.events if event[0] == "camera_frame"]
        self.assertTrue(all(b - a >= 32 for a, b in zip(timestamps, timestamps[1:])))
        self.assertTrue(stream.snapshot()["auto_exposure"])

    def test_slow_encoding_does_not_burst_preview_delivery(self):
        stream = self.make_stream(Mock(return_value=FakeCapture()))
        delivered = []
        original_emit = stream.emit
        encode = cv2.imencode
        calls = 0

        def delayed_encode(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                threading.Event().wait(0.09)
            return encode(*args)

        def record_delivery(name, payload):
            if name == "camera_frame":
                delivered.append(time.monotonic())
            original_emit(name, payload)

        stream.emit = record_delivery
        with patch.object(camera_stream.cv2, "imencode", side_effect=delayed_encode):
            stream.start()
            wait_for(lambda: len(delivered) >= 4)
            stream.stop()
        self.assertTrue(all(b - a >= 0.032 for a, b in zip(delivered, delivered[1:])), delivered)

    def test_existing_device_owner_is_preserved_without_open(self):
        opened = Mock()
        stream = self.make_stream(opened)
        stream.owner_check = lambda _: True
        stream.start()
        wait_for(lambda: stream.snapshot()["error"] is not None)
        opened.assert_not_called()
        self.assertIn("他のアプリ", stream.snapshot()["error"])

    def test_no_device_and_arbitrary_source_rejected_before_open(self):
        opened = Mock()
        stream = self.make_stream(opened)
        with self.assertRaises(ValueError):
            stream.start("http://camera/stream")
        stream.devices = lambda: []
        with self.assertRaisesRegex(ValueError, "カメラがありません"):
            stream.start()
        opened.assert_not_called()


class SourceTests(unittest.TestCase):
    def test_only_explicit_usb_nodes_are_parsed(self):
        self.assertEqual(parse_usb_source("4"), 4)
        self.assertEqual(parse_usb_source("/dev/video4"), "/dev/video4")
        for source in ("http://camera", "-1", "/etc/passwd", "v4l2:///dev/video4"):
            with self.assertRaises(ValueError):
                parse_usb_source(source)

    def test_imports_do_not_load_legacy_or_models_or_open_hardware(self):
        code = """
import sys
sys.path.insert(0, 'host')
import cv2
cv2.VideoCapture = lambda *a, **k: (_ for _ in ()).throw(AssertionError('camera opened'))
import app, recognition, camera_stream, desktop_backend
assert not any(name in sys.modules for name in ('tkinter', 'serial', 'pytesseract', 'rapidocr', 'onnxruntime'))
assert recognition._RAPIDOCR_ENGINE is None
"""
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
