"""표준 라이브러리와 가짜 장치만 사용한다. GPIO/I2C를 실행하지 않는다.
실행: python -m unittest discover -s picar_team/tests -v (저장소 최상위)
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch


TEAM = Path(__file__).resolve().parents[1]
ROOT = TEAM.parent
TEST_TEMP = TEAM / ".test_tmp"
sys.path.insert(0, str(TEAM))
import practice_logger as pl


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


web = load_module("web_status_test", ROOT / "web_monitor" / "web_status.py")


def wait_until(condition, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if condition():
                return
        except (FileNotFoundError, PermissionError):
            pass  # Windows에서 원자적 교체 중 읽기가 잠깐 거부될 수 있다.
        time.sleep(0.01)
    raise AssertionError("condition timed out")


class RecordingTests(unittest.TestCase):
    def setUp(self):
        TEST_TEMP.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=TEST_TEMP)
        self.directory = Path(self.temporary.name)
        self.recorders = []
        self.state = {"servo_deg": {"steer": 90}, "motor_throttle": {"M1": 0},
                      "distance_cm": 30.0, "phase": "test"}

    def tearDown(self):
        for recorder in self.recorders:
            recorder.close()
        self.temporary.cleanup()

    def recorder(self, **kwargs):
        recorder = pl.PracticeRecorder(self.directory, "test", lambda: self.state,
                                       interval_s=0.02, flush_s=0.03, **kwargs)
        self.recorders.append(recorder)
        return recorder

    def read(self, recorder):
        return json.loads(recorder.path.read_text(encoding="utf-8"))

    def test_periodic_checkpoint_and_final_metadata(self):
        config = {"speed": 25}
        recorder = self.recorder(config=config)
        config["speed"] = 99
        wait_until(lambda: len(self.read(recorder)["samples"]) >= 2)
        self.assertEqual(self.read(recorder)["run_status"], "running")
        recorder.close("interrupted")
        data = self.read(recorder)
        self.assertEqual(data["run_status"], "interrupted")
        self.assertEqual(data["config_snapshot"]["speed"], 25)
        self.assertIsNone(data["result"]["success"])
        self.assertIsNotNone(data["ended_at"])
        self.assertEqual([s["seq"] for s in data["samples"]], list(range(len(data["samples"]))))
        times = [s["elapsed_s"] for s in data["samples"]]
        self.assertEqual(times, sorted(times))

    def test_short_pulse_and_snapshot_copy(self):
        recorder = self.recorder()
        self.state["motor_throttle"]["M1"] = -0.15
        recorder.record("motor")
        self.state["motor_throttle"]["M1"] = 0
        recorder.record("stop")
        recorder.close()
        commands = {s["event"]: s for s in self.read(recorder)["samples"] if s["kind"] == "command"}
        self.assertEqual(commands["motor"]["motor_throttle"]["M1"], -0.15)
        self.assertEqual(commands["stop"]["motor_throttle"]["M1"], 0)

    def test_numbering_does_not_overwrite_existing_files(self):
        original = '{"keep":true}'
        (self.directory / "practice1.json").write_text(original)
        (self.directory / "practice4.json").write_text(original)
        recorder = self.recorder()
        self.assertEqual(recorder.path.name, "practice5.json")
        recorder.close()
        self.assertEqual((self.directory / "practice1.json").read_text(), original)

    def test_simultaneous_sessions_get_unique_filenames(self):
        barrier = threading.Barrier(3)
        found = []
        def create():
            barrier.wait()
            item = self.recorder()
            found.append(item.path)
        threads = [threading.Thread(target=create) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(found)), 3)

    def test_invalid_numbers_become_json_null(self):
        self.state["distance_cm"] = float("nan")
        recorder = self.recorder()
        recorder.close()
        self.assertIsNone(self.read(recorder)["samples"][0]["distance_cm"])

    def test_failed_write_keeps_previous_json_and_records_can_continue(self):
        recorder = self.recorder()
        recorder.done.set()
        recorder.thread.join()
        before = recorder.path.read_bytes()
        with patch.object(pl.os, "replace", side_effect=OSError("disk full")):
            self.assertFalse(recorder.save())
            recorder.record("after_io_error")
        self.assertEqual(recorder.path.read_bytes(), before)
        recorder.close()
        self.assertTrue(any(s["event"] == "after_io_error" for s in self.read(recorder)["samples"]))
        self.assertFalse(list(self.directory.glob("*.tmp")))

    def test_annotation_cli_preserves_samples(self):
        recorder = self.recorder()
        recorder.close()
        before = self.read(recorder)["samples"]
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(TEAM / "practice_logger.py"),
                                 "annotate", str(recorder.path), "--success", "yes",
                                 "--stop-error-cm", "-1.2", "--notes", "measured"],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.read(recorder)
        self.assertEqual(data["samples"], before)
        self.assertTrue(data["result"]["success"])
        self.assertEqual(data["result"]["stop_error_cm"], -1.2)


class WebTests(unittest.TestCase):
    def setUp(self):
        TEST_TEMP.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=TEST_TEMP)
        self.directory = Path(self.temporary.name)
        self.created = 0
        self.sensor_release = threading.Event()
        class Servo:
            def set_angle(self, ID, angle):
                if angle < 0:
                    raise ValueError("servo error")
                return "servo-return"
        self.Servo = Servo
        self.original_angle = Servo.set_angle
        self.move = types.SimpleNamespace()
        for i in range(1, 5):
            setattr(self.move, f"motor{i}", types.SimpleNamespace(throttle=0))
        def motor(channel, direction, motor_speed):
            if motor_speed == 999:
                raise ValueError("motor error")
            value = max(0, min(100, motor_speed)) / 100
            getattr(self.move, f"motor{channel}").throttle = -value if direction == -1 else value
            return "motor-return"
        def stop():
            for i in range(1, 5):
                getattr(self.move, f"motor{i}").throttle = 0
        self.move.Motor, self.move.motorStop = motor, stop
        self.original_motor = motor
        def distance():
            self.sensor_release.wait()
            return 31.0
        self.monitor = web.StatusMonitor(types.SimpleNamespace(ServoCtrl=Servo), self.move,
                                        types.SimpleNamespace(checkdist=distance), lambda: 25,
                                        interval=0.1, writer=lambda line: None,
                                        recording={"directory": str(self.directory),
                                                   "interval_s": 0.02, "flush_s": 0.03})
        self.monitor.start()

    def tearDown(self):
        self.sensor_release.set()
        self.monitor.stop()
        self.temporary.cleanup()

    def request(self, action, label=""):
        request = {"id": str(time.monotonic_ns()), "created_at": time.time(),
                   "action": action, "label": label}
        pl.atomic_json(self.directory / ".record-request.json", request)
        path = self.directory / ".record-response.json"
        wait_until(lambda: path.exists() and json.loads(path.read_text())["id"] == request["id"])
        return json.loads(path.read_text())

    def test_manual_sessions_and_short_commands_with_blocked_sensor(self):
        self.assertFalse(list(self.directory.glob("practice*.json")))
        first = self.request("start", "first")
        self.assertTrue(first["ok"])
        servo = self.Servo()
        self.assertEqual(servo.set_angle(1, 123), "servo-return")
        self.assertEqual(self.move.Motor(1, -1, 20), "motor-return")
        self.move.motorStop()
        wait_until(lambda: len(self.monitor.recording.recorder.data["samples"]) >= 4)
        duplicate = self.request("start")
        self.assertFalse(duplicate["ok"])
        self.assertTrue(self.request("stop")["ok"])
        data = json.loads(Path(first["path"]).read_text(encoding="utf-8"))
        self.assertEqual(data["label"], "first")
        self.assertEqual(data["run_status"], "manual_stopped")
        self.assertTrue(any(s["servo_deg"]["base"] == 123 for s in data["samples"]))
        self.assertTrue(any(s["motor_throttle"]["M1"] == -0.2 for s in data["samples"]))
        self.assertEqual(data["samples"][-1]["motor_throttle"]["M1"], 0)
        self.assertTrue(all(s["distance_cm"] is None for s in data["samples"]))
        second = self.request("start")
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(self.request("stop")["ok"])

    def test_failed_hardware_commands_are_not_recorded_as_success(self):
        self.request("start")
        with self.assertRaises(ValueError):
            self.Servo().set_angle(1, -1)
        with self.assertRaises(ValueError):
            self.move.Motor(1, 1, 999)
        state = self.monitor.recording_snapshot()
        self.assertIsNone(state["servo_deg"]["base"])
        self.assertIsNone(state["motor_throttle"]["M1"])
        self.monitor.stop()
        self.assertIs(self.Servo.set_angle, self.original_angle)
        self.assertIs(self.move.Motor, self.original_motor)

    def test_ssh_cli_gets_acknowledgement(self):
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(TEAM / "practice_logger.py"),
                                 "start", "--directory", str(self.directory), "--label", "cli"],
                                capture_output=True, text=True, encoding="utf-8", timeout=7)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.monitor.recording.recorder.data["label"], "cli")
        self.request("stop")

    def test_stale_command_is_not_replayed(self):
        request = {"id": "old", "created_at": time.time() - 30, "action": "start"}
        pl.atomic_json(self.directory / ".record-request.json", request)
        wait_until(lambda: (self.directory / ".record-response.json").exists())
        self.assertIsNone(self.monitor.recording.recorder)


def fake_import_hw():
    """라이브러리 이름만 가짜 모듈로 대체하고 실제 장치 드라이버는 읽지 않는다."""
    servo_module = types.ModuleType("adafruit_motor.servo")
    motor_module = types.ModuleType("adafruit_motor.motor")
    fake_motor = types.ModuleType("adafruit_motor")
    fake_motor.servo, fake_motor.motor = servo_module, motor_module
    modules = {"board": types.SimpleNamespace(SCL=1, SDA=2), "busio": types.ModuleType("busio"),
               "adafruit_pca9685": types.SimpleNamespace(PCA9685=None),
               "adafruit_motor": fake_motor,
               "gpiozero": types.SimpleNamespace(DistanceSensor=None), "cv2": types.ModuleType("cv2")}
    with patch.dict(sys.modules, modules):
        return load_module("hw_test", TEAM / "hw.py")


class HardwareIntegrationTests(unittest.TestCase):
    def setUp(self):
        TEST_TEMP.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=TEST_TEMP)
        self.directory = Path(self.temporary.name)
        self.hw = fake_import_hw()
        self.hw.cfg = json.loads((TEAM / "config.json").read_text(encoding="utf-8"))
        self.hw.cfg["verbose"] = False

    def tearDown(self):
        if self.hw.recorder is not None:
            self.hw.close()
        self.temporary.cleanup()

    def test_hw_events_include_limited_angle_and_signed_motor_command(self):
        hw = self.hw
        hw.motor_left = types.SimpleNamespace(throttle=None)
        hw.motor_right = types.SimpleNamespace(throttle=None)
        hw.servos["steer"] = types.SimpleNamespace(angle=None)
        recorder = pl.PracticeRecorder(self.directory, "test", hw.recording_snapshot)
        hw.recorder = recorder
        hw.set_servo("steer", 150)
        hw.drive(25)
        hw.reset_clock()
        hw.stop()
        hw.close("interrupted")
        data = json.loads(recorder.path.read_text(encoding="utf-8"))
        self.assertTrue(any(s["servo_deg"]["steer"] == 120 for s in data["samples"]))
        self.assertTrue(any(s["motor_throttle"]["M1"] == -0.25 for s in data["samples"]))
        self.assertEqual(data["samples"][-1]["speed_command"], 0)
        self.assertEqual(data["samples"][-1]["motor_throttle"]["M2"], 0)
        self.assertEqual(data["run_status"], "interrupted")

    def test_init_records_distance_while_main_thread_is_idle(self):
        hw = self.hw
        cfg = hw.cfg
        cfg["recording"].update(directory=str(self.directory), interval_s=0.02, flush_s=0.03)
        class Sensor:
            reads = 0
            @property
            def distance(self):
                self.reads += 1
                return 0.42
            def close(self):
                pass
        sensor = Sensor()
        with patch.object(hw, "load_config", return_value=cfg), \
             patch.object(hw.busio, "I2C", lambda *args: object(), create=True), \
             patch.object(hw, "PCA9685", lambda *a, **kw: types.SimpleNamespace(channels=list(range(16)))), \
             patch.object(hw.ada_servo, "Servo", lambda *a, **kw: types.SimpleNamespace(angle=None), create=True), \
             patch.object(hw.ada_motor, "DCMotor", lambda *a: types.SimpleNamespace(throttle=None), create=True), \
             patch.object(hw.ada_motor, "SLOW_DECAY", 1, create=True), \
             patch.object(hw, "DistanceSensor", lambda **kw: sensor):
            hw.init(use_camera=False, recording_mode="manual_servo")
            wait_until(lambda: sensor.reads >= 3)
            path = hw.recorder.path
            hw.close()
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], "manual_servo")
        self.assertTrue(any(s["distance_cm"] == 42 for s in data["samples"]))

    def test_mission_initialization_failure_closes_recording(self):
        hw = self.hw
        with patch.dict(sys.modules, {"hw": hw}):
            mission = load_module("mission_test", TEAM / "mission.py")
        def fail_init(**kwargs):
            hw.recorder = pl.PracticeRecorder(self.directory, "mission", hw.recording_snapshot)
            raise RuntimeError("I2C unavailable")
        with patch.object(hw, "load_config", return_value={}), patch.object(hw, "init", side_effect=fail_init):
            with self.assertRaisesRegex(RuntimeError, "I2C unavailable"):
                mission.main()
        data = json.loads((self.directory / "practice1.json").read_text(encoding="utf-8"))
        self.assertEqual(data["run_status"], "error")
        self.assertIn("I2C unavailable", data["error"])

    def test_distance_failure_keeps_last_read_and_error(self):
        hw = self.hw
        hw.sonar = types.SimpleNamespace(distance=0.3)
        self.assertEqual(hw.get_distance(), 30)
        hw.sonar.distance = float("nan")
        with self.assertRaises(ValueError):
            hw.get_distance()
        self.assertEqual(hw.last_distance, 30)
        self.assertIn("ValueError", hw.distance_error)


if __name__ == "__main__":
    unittest.main()
