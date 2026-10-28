"""Adeept 웹 조종 프로그램 안에서 상태를 출력하는 보조 모듈.

web_status.py, practice_logger.py, recording.json을 실제 WebServer.py 옆에 놓는다.
WebServer.py에서 speed_set과 rad를 정한 뒤, scGear를 만들기 전에 추가:

    import web_status
    web_status.start(RPIservo, move, functions.ultra, lambda: speed_set)

단독 실행하는 파일이 아니다. GPIO/PCA9685/센서 객체를 새로 만들지 않는다.
서보와 모터의 값은 실제 각도/속도가 아닌 명령값이다.
distance_read_age_s는 '이 모듈이 값을 읽은 뒤 지난 시간'이며,
초음파 라이브러리 내부의 실제 샘플 생성 시각을 뜻하지 않는다.
시연 JSON: SSH에서 python3 practice_logger.py start / stop으로 기록만 제어한다.
"""

import atexit
import functools
import math
import threading
import time
import json
from pathlib import Path

from practice_logger import WebRecordingControl


SERVO_NAMES = ("steer", "base", "arm", "wrist", "grip")
_active_monitor = None


def _print_line(line):
    print(line, flush=True)


def _number(value, digits=1):
    return "?" if value is None else f"{value:.{digits}f}"


class StatusMonitor:
    """기존 객체를 관찰한다. start/stop은 상태 출력만 시작/종료한다."""

    def __init__(self, servo_module, move_module, ultra_module,
                 speed_getter, interval=0.5, writer=None, recording=None):
        if not math.isfinite(interval) or interval < 0.1:
            raise ValueError("interval은 0.1초 이상의 유한한 수여야 합니다.")
        if not callable(speed_getter):
            raise TypeError("speed_getter에는 lambda: speed_set을 전달하세요.")
        if not callable(getattr(ultra_module, "checkdist", None)):
            raise TypeError("checkdist()가 있는 기존 Ultra 모듈이 필요합니다.")

        self.servo_class = servo_module.ServoCtrl
        self.original_set_angle = self.servo_class.set_angle
        self.move_module = move_module
        self.ultra_module = ultra_module
        self.speed_getter = speed_getter
        self.interval = interval
        self.writer = writer or _print_line
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.angles = {}
        self.distance_cm = None
        self.distance_read_at = None
        self.distance_error = None
        self.threads = []
        self.wrapped_set_angle = None
        self.motor_commands = {f"M{i}": None for i in range(1, 5)}
        self.move_wrappers = []
        base = Path(__file__).resolve().parent
        if recording is None:
            path = base / "recording.json"
            recording = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.recording = WebRecordingControl(
            base, self.recording_snapshot, recording,
            config={"recording": recording, "servo_names": list(SERVO_NAMES),
                    "source": "existing Adeept WebServer; team config.json is not used"})

    def start(self):
        if self.threads:
            return self

        # 원래 명령을 그대로 실행한 뒤 성공한 호출의 각도만 기억한다.
        # 클래스에 연결하므로 원본 안의 여러 ServoCtrl 객체도 관찰한다.
        @functools.wraps(self.original_set_angle)
        def record_angle(controller, ID, angle):
            result = self.original_set_angle(controller, ID, angle)
            if 0 <= ID < len(SERVO_NAMES):
                with self.lock:
                    self.angles[SERVO_NAMES[ID]] = angle
                self.recording.record("servo", {"name": SERVO_NAMES[ID], "angle_deg": angle})
            return result

        self.wrapped_set_angle = record_angle
        self.servo_class.set_angle = record_angle
        self._watch_move_commands()
        self.writer("[MONITOR] 서보/모터는 명령값입니다. 실제 각도/주행 속도를 측정하지 않습니다.")
        self.writer("[MONITOR] distance_read_age_s는 읽기 시각 기준입니다. 센서 샘플의 최신성을 보장하지 않습니다.")
        self.writer(f"[RECORD] 웹 시연 기록 대기: {self.recording.directory} (SSH에서 practice_logger.py start/stop)")

        # 초음파 첫 측정이 대기 중이어도 서보/모터 상태 출력은 계속한다.
        for target, name in ((self._read_distance_loop, "picar-distance-log"),
                             (self._display_loop, "picar-status-log")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self.threads.append(thread)
            thread.start()
        return self

    def _watch_move_commands(self):
        """제공된 Move.py의 성공한 Motor/motorStop/setup 호출을 관찰한다."""
        def attach(name, wrapper_factory):
            original = getattr(self.move_module, name, None)
            if callable(original):
                wrapped = wrapper_factory(original)
                setattr(self.move_module, name, wrapped)
                self.move_wrappers.append((name, original, wrapped))

        def motor_wrapper(original):
            @functools.wraps(original)
            def wrapped(channel, direction, motor_speed):
                result = original(channel, direction, motor_speed)
                if channel in range(1, 5):
                    # Move.Motor()와 같은 제한/부호. 실제 회전속도를 뜻하지 않는다.
                    throttle = max(0, min(100, motor_speed)) / 100
                    if direction == -1:
                        throttle = -throttle
                    with self.lock:
                        changed = self.motor_commands[f"M{channel}"] != throttle
                        self.motor_commands[f"M{channel}"] = throttle
                    if changed:
                        self.recording.record("motor", {"name": f"M{channel}", "throttle": throttle})
                return result
            return wrapped

        def reset_wrapper(original, value, event):
            @functools.wraps(original)
            def wrapped(*args, **kwargs):
                result = original(*args, **kwargs)
                with self.lock:
                    self.motor_commands = {f"M{i}": value for i in range(1, 5)}
                self.recording.record(event)
                return result
            return wrapped

        attach("Motor", motor_wrapper)
        attach("motorStop", lambda original: reset_wrapper(original, 0, "motor_stop"))
        attach("setup", lambda original: reset_wrapper(original, None, "motor_setup"))

    def recording_snapshot(self):
        # GPIO/I2C를 읽지 않고 성공한 명령과 거리 캐시만 사용한다.
        with self.lock:
            state = {
                "phase": "web_manual",
                "servo_deg": {name: self.angles.get(name) for name in SERVO_NAMES},
                "motor_throttle": dict(self.motor_commands),
                "speed_command": None, "wheels_locked": None,
                "distance_cm": self.distance_cm,
                "distance_read_age_s": (None if self.distance_read_at is None
                                        else round(time.monotonic() - self.distance_read_at, 4)),
                "distance_error": self.distance_error,
            }
        try:
            state["speed_setting"] = self.speed_getter()
        except Exception:
            state["speed_setting"] = None
        return state

    def _read_distance_loop(self):
        while not self.done.is_set():
            try:
                # Functions.py가 이미 사용하는 Ultra.sensor를 재사용한다.
                value = float(self.ultra_module.checkdist())
                if not math.isfinite(value) or value < 0:
                    raise ValueError("유효하지 않은 거리 값")
                with self.lock:
                    self.distance_cm = value
                    self.distance_read_at = time.monotonic()
                    self.distance_error = None
            except Exception as exc:
                with self.lock:
                    self.distance_error = type(exc).__name__
            if self.done.wait(self.interval):
                return

    def _motor_command(self, name):
        # Move.setup()이 객체를 다시 만들어도 현재 객체를 그때그때 읽는다.
        motor = getattr(self.move_module, name, None)
        if motor is None:
            return "?"
        try:
            value = motor.throttle
            return "None" if value is None else f"{value:+.2f}"
        except Exception:
            return "?"

    def snapshot(self):
        with self.lock:
            angles = dict(self.angles)
            distance = self.distance_cm
            read_at = self.distance_read_at
            error = self.distance_error
        try:
            speed = self.speed_getter()
        except Exception:
            speed = "?"
        return {
            "angles": angles,
            "speed_setting": speed,
            "M1_cmd": self._motor_command("motor1"),
            "M2_cmd": self._motor_command("motor2"),
            "distance_cm": distance,
            "distance_read_age_s": None if read_at is None else time.monotonic() - read_at,
            "distance_error": error,
        }

    def _display_loop(self):
        while not self.done.is_set():
            self.recording.poll()
            state = self.snapshot()
            angles = " ".join(
                f"{name}_cmd={_number(state['angles'].get(name))}"
                for name in SERVO_NAMES
            )
            if state["distance_error"]:
                distance_status = "error:" + state["distance_error"]
            elif state["distance_cm"] is None:
                distance_status = "waiting"
            elif state["distance_read_age_s"] > max(2.0, self.interval * 3):
                distance_status = "read_delayed"
            else:
                distance_status = "read_ok"
            line = (
                f"[STATE {time.strftime('%H:%M:%S')}] {angles}"
                f" | speed_setting={state['speed_setting']}"
                f" M1_cmd={state['M1_cmd']} M2_cmd={state['M2_cmd']}"
                f" | distance_cm={_number(state['distance_cm'], 2)}"
                f" distance_read_age_s={_number(state['distance_read_age_s'])}"
                f" distance_status={distance_status}"
            )
            self.writer(line)
            if self.done.wait(self.interval):
                return

    def stop(self):
        """출력만 종료한다. 로봇 정지는 기존 웹 조종 프로그램에서 해야 한다."""
        self.done.set()
        if self.servo_class.set_angle is self.wrapped_set_angle:
            self.servo_class.set_angle = self.original_set_angle
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=0.5)
        for name, original, wrapped in self.move_wrappers:
            if getattr(self.move_module, name) is wrapped:
                setattr(self.move_module, name, original)
        self.recording.close()


def start(servo_module, move_module, ultra_module, speed_getter, interval=0.5, recording=None):
    """WebServer.py에서 초기 자세를 명령하기 전에 한 번 호출한다."""
    global _active_monitor
    if _active_monitor is not None and not _active_monitor.done.is_set():
        return _active_monitor
    monitor = StatusMonitor(servo_module, move_module, ultra_module,
                            speed_getter, interval, recording=recording)
    monitor.start()
    _active_monitor = monitor
    atexit.register(monitor.stop)
    return monitor


if __name__ == "__main__":
    print(__doc__)
    print("이 파일을 단독 실행해도 실행 중인 웹서버의 상태를 읽을 수 없습니다.")
