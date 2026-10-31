"""
hw.py — PiCar의 "몸": 하드웨어를 움직이고 읽는 함수만 둔다.

규칙
- 미션 순서·판단("왼쪽부터" 등)은 여기 넣지 않는다 → mission.py
- 하드웨어 객체(PCA9685, 초음파, 카메라)는 init()에서 딱 한 번만 만든다
- 숫자(각도·속도·거리)는 config.json에서 읽는다
- 속도·서보 각도·거리가 바뀔 때마다 터미널에 상태 한 줄을 출력한다 (config "verbose")

단독 테스트 (Pi에서, 이 폴더 안에서 실행)
    python3 hw.py servo              # 서보 각도 직접 입력 (자세 측정 도구)
    python3 hw.py motor [속도] [초]   # 예) python3 hw.py motor 30 1.0  → 30%로 1초 전진 후 정지
    python3 hw.py dist               # 초음파 거리 계속 출력
    python3 hw.py qr                 # 카메라로 QR 읽기
"""
import json
import os
import sys
import time
import math
import threading

from practice_logger import PracticeRecorder, settings as recording_settings
import qr_read

# Adeept 코드와 같은 라이브러리 (picar source code/Servo180Degree.py 14~18행, Move.py 8~11행)
from board import SCL, SDA
import busio
from adafruit_pca9685 import PCA9685
from adafruit_motor import servo as ada_servo
from adafruit_motor import motor as ada_motor
from gpiozero import DistanceSensor
import cv2

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
SERVO_NAMES = ["steer", "base", "arm", "wrist", "grip"]

# ---- 하드웨어 객체: init()에서 채운다. 다른 곳에서 새로 만들지 않는다 ----
cfg = None            # config.json 내용
pca = None            # PCA9685 칩 1개 — 모터와 서보가 함께 쓴다
servos = {}           # {"steer": 서보객체, "base": ..., "arm": ..., "wrist": ..., "grip": ...}
servo_angles = {}     # 각 서보에 마지막으로 보낸 각도 (부드럽게 움직일 때 출발점)
motor_left = None     # M1
motor_right = None    # M2
sonar = None          # 초음파
camera = None         # USB 웹캠
buzzer = None         # 완료 신호용 (없어도 동작)
frame_width = None    # 웹캠이 실제로 주는 가로 해상도 (init에서 채움)

wheels_locked = False  # True가 되면 drive()가 바퀴를 절대 움직이지 않는다 (정지 후 실격 방지)
current_speed = 0      # 마지막으로 보낸 바퀴 속도 (-100~100)
last_distance = None   # 마지막으로 읽은 거리(cm)
T0 = time.time()       # 터미널 출력 시간 기준
recorder = None
phase = "idle"
motor_commands = {"M1": None, "M2": None}
distance_read_at = None
distance_error = None
_distance_done = threading.Event()
_distance_thread = None


def recording_snapshot():
    """캐시만 읽는다. JSON 쓰기 스레드에서 GPIO/I2C를 제어하지 않는다."""
    return {
        "phase": phase,
        "servo_deg": {n: servo_angles.get(n) for n in SERVO_NAMES},
        "motor_throttle": dict(motor_commands),
        "speed_command": current_speed,
        "speed_setting": None,
        "wheels_locked": wheels_locked,
        "distance_cm": last_distance,
        "distance_read_age_s": (None if distance_read_at is None
                                else round(time.monotonic() - distance_read_at, 4)),
        "distance_error": distance_error,
    }


def record_event(event, details=None):
    if recorder is not None:
        commands = {"servo", "motor", "motor_stop", "drive", "stop"}
        recorder.record(event, details, kind="command" if event in commands else "event")


def set_phase(name, **details):
    global phase
    phase = name
    record_event("phase", details)


def _distance_logging_loop(interval):
    # 팔 동작이나 사용자 입력 대기 중에도 거리를 읽는다.
    # GPIO Zero의 기존 센서 객체를 재사용하며 별도 센서를 만들지 않는다.
    while not _distance_done.is_set():
        try:
            get_distance()
        except Exception:
            pass  # get_distance가 오류 상태를 캐시에 남긴다.
        if _distance_done.wait(interval):
            return


def load_config(path=CONFIG_PATH):
    """config.json을 읽어 dict로 돌려준다."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# =====================================================================
# 터미널 상태 출력
# =====================================================================
def elapsed():
    return time.time() - T0


def reset_clock():
    """시간 기준을 지금으로 (QR 인식 순간에 호출 → 이후 시간 = 경기 기록에 가까운 값)"""
    global T0
    T0 = time.time()


def status_line(tag=""):
    """예) [  1.23s] drive  speed= +50 | steer= 90.0 base= 90.0 arm= 60.0 wrist= 90.0 grip= 40.0 | dist= 45.2cm"""
    parts = []
    for name in SERVO_NAMES:
        a = servo_angles.get(name)
        parts.append(f"{name}={a:5.1f}" if a is not None else f"{name}=  -  ")
    dist = f"{last_distance:5.1f}cm" if last_distance is not None else "  -  "
    lock = " [LOCK]" if wheels_locked else ""
    return (f"[{elapsed():6.2f}s] {tag:<6} speed={current_speed:+4.0f}{lock} | "
            + " ".join(parts) + f" | dist={dist}")


def show_status(tag=""):
    if cfg is None or cfg.get("verbose", True):
        print(status_line(tag), flush=True)


# =====================================================================
# 초기화 / 종료
# =====================================================================
def init(use_camera=True, recording_mode="manual"):
    """모든 하드웨어를 한 번만 준비한다. 프로그램 시작 때 1회 호출.
    use_camera=False: 카메라 없이 준비 (서보·모터·초음파만 테스트할 때)
    """
    global cfg, pca, motor_left, motor_right, sonar, camera, buzzer
    global recorder, phase, _distance_thread, wheels_locked, current_speed
    global last_distance, distance_read_at, distance_error
    cfg = load_config()
    servo_angles.clear()
    motor_commands.update(M1=None, M2=None)
    wheels_locked, current_speed = False, 0
    last_distance = distance_read_at = distance_error = None
    phase = "initializing"
    options = recording_settings(os.path.dirname(CONFIG_PATH), cfg.get("recording"))
    if options["enabled"]:
        recorder = PracticeRecorder(options["directory"], recording_mode, recording_snapshot,
                                    config=cfg, interval_s=options["interval_s"],
                                    flush_s=options["flush_s"])

    # 1) PCA9685: I2C 주소 95 = 0x5f. 모터·서보가 같은 칩이라 둘 다 50Hz
    i2c = busio.I2C(SCL, SDA)
    pca = PCA9685(i2c, address=cfg["pca9685_address"])
    pca.frequency = 50

    # 2) 서보 5개 (Servo180Degree.py 28행과 같은 설정)
    lo, hi = cfg["servo_pulse_us"]
    for name, ch in cfg["servo_channel"].items():
        servos[name] = ada_servo.Servo(pca.channels[ch], min_pulse=lo, max_pulse=hi,
                                       actuation_range=180)

    # 3) 모터 2개 (Move.py 50~53행). SLOW_DECAY = 저속에서 안정적, throttle 0 = 브레이크
    m1 = cfg["motor"]["channels"]["m1"]
    m2 = cfg["motor"]["channels"]["m2"]
    motor_left = ada_motor.DCMotor(pca.channels[m1[0]], pca.channels[m1[1]])
    motor_right = ada_motor.DCMotor(pca.channels[m2[0]], pca.channels[m2[1]])
    motor_left.decay_mode = ada_motor.SLOW_DECAY
    motor_right.decay_mode = ada_motor.SLOW_DECAY

    # 4) 초음파: queue_len이 작을수록 반응이 빠르다 (기본 9는 0.2~0.3초 늦음)
    s = cfg["sonar"]
    sonar = DistanceSensor(echo=s["echo"], trigger=s["trig"],
                           max_distance=s["max_distance_m"], queue_len=s["queue_len"])
    if recorder is not None:
        _distance_done.clear()
        _distance_thread = threading.Thread(target=_distance_logging_loop,
                                            args=(options["interval_s"],), daemon=True)
        _distance_thread.start()

    # 5) USB 웹캠 + QR 검출기
    if use_camera:
        c = cfg["camera"]
        camera = cv2.VideoCapture(c["index"], cv2.CAP_V4L2)   # GStreamer는 해상도·버퍼 설정을 무시한다
        if not camera.isOpened():
            raise RuntimeError(f"카메라 {c['index']}번을 열 수 없습니다. (ls /dev/video* 로 번호 확인)")
        camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, c["width"])
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, c["height"])
        camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        global frame_width
        frame_width = int(camera.get(cv2.CAP_PROP_FRAME_WIDTH)) or c["width"]   # 카메라가 해상도를 거부하면 실제 값    # 오래된 프레임이 쌓이지 않게 (지원 안 되면 무시됨)

    # 6) 부저 (Buzzer.py: GPIO18). 실패해도 미션에는 지장 없음
    try:
        from gpiozero import TonalBuzzer
        buzzer = TonalBuzzer(18, octaves=2)   # 기본(1옥타브, A3~A5)으로는 C6을 낼 수 없다
    except Exception:
        buzzer = None

    # 7) 바퀴 정지, 조향 중앙
    stop()
    set_servo("steer", cfg["steer_center"])
    show_status("init")


def close(status="ended", error=None):
    """종료 정리: 모터 정지 → 초음파·카메라 해제.
    PCA9685는 deinit 하지 않는다: 끄면 서보 힘이 빠져 팔이 툭 떨어진다.
    (프로그램이 끝나도 칩은 마지막 각도를 계속 유지한다)
    """
    global recorder
    cleanup_error = None
    try:
        stop()
    except Exception as exc:
        cleanup_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _distance_done.set()
        try:
            if _distance_thread is not None:
                _distance_thread.join(timeout=0.2)
            try:
                if sonar is not None:
                    sonar.close()
            finally:
                if camera is not None:
                    camera.release()
        except Exception as exc:
            cleanup_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if recorder is not None:
                current, recorder = recorder, None
                current.close("error" if cleanup_error else status, error or cleanup_error)


# =====================================================================
# 서보
# =====================================================================
def set_servo(name, angle, show=True):
    """서보 하나를 각도로 즉시 이동. name = "steer" / "base" / "arm" / "wrist" / "grip"
    cfg["servo_limits"] 범위 밖이면 범위 안으로 자른다 (기구 보호).
    """
    lo, hi = cfg["servo_limits"][name]
    angle = max(lo, min(hi, float(angle)))
    servos[name].angle = angle
    servo_angles[name] = angle
    record_event("servo", {"name": name, "angle_deg": angle})
    if show:
        show_status(name)


def move_servos(target, duration=None):
    """여러 서보를 '동시에' 부드럽게 이동하고, 도착하면 돌아온다.
    target 예: {"base": 120, "arm": 60, "wrist": 90}
    duration: 이동 시간(초). None이면 cfg["servo_move_time"], 0이면 즉시 이동
    원리: 출발각→목표각을 여러 단계로 나눠 조금씩 이동 (Servo180Degree.py 33~35행)
          모든 관절을 같은 단계 수로 나누므로 동시에 출발·도착한다
    """
    if duration is None:
        duration = cfg["servo_move_time"]
    step_s = cfg["servo_step_s"]
    steps = max(1, int(round(duration / step_s)))
    # 처음 움직이는 서보는 현재 각도를 모르므로 목표로 바로 이동
    start = {n: servo_angles.get(n, target[n]) for n in target}
    for i in range(1, steps + 1):
        for n in target:
            a = start[n] + (target[n] - start[n]) * i / steps
            set_servo(n, a, show=False)
        if steps > 1:
            time.sleep(step_s)
    show_status("arm")


def grip(state, angle=None):
    """state = "open" 또는 "close" → cfg["grip"][state] 각도(angle을 주면 그 각도)로 grip_move_s 동안 천천히 움직인 뒤 grip_wait_s 대기.
    서보는 현재 각도를 알려주지 않으므로 끝까지 열리고·닫히도록 시간을 넉넉히 준다 (성능 개선 때 줄인다).
    이번 실행에서 집게에 처음 보내는 명령이면 시작 각도를 몰라 바로 보내고, 그만큼 더 기다린다."""
    target = cfg["grip"][state] if angle is None else angle
    if "grip" in servo_angles:
        move_servos({"grip": target}, cfg["grip_move_s"])
    else:
        set_servo("grip", target)
        time.sleep(cfg["grip_move_s"])
    time.sleep(cfg["grip_wait_s"])


# =====================================================================
# 바퀴
# =====================================================================
def drive(speed):
    """speed: -100 ~ 100 (양수 전진, 음수 후진). 직진만 한다.
    wheels_locked 이면 아무것도 하지 않는다 ← 실격 방지 안전장치
    """
    global current_speed
    if wheels_locked:
        return
    speed = max(-100, min(100, speed))
    throttle = speed / 100 * cfg["motor"]["direction"]   # Move.py: M1_Direction = -1
    motor_left.throttle = throttle
    if motor_commands["M1"] != throttle:
        motor_commands["M1"] = throttle
        record_event("motor", {"name": "M1", "throttle": throttle})
    motor_right.throttle = throttle
    if motor_commands["M2"] != throttle:
        motor_commands["M2"] = throttle
        record_event("motor", {"name": "M2", "throttle": throttle})
    if speed != current_speed:
        current_speed = speed
        show_status("drive")
        record_event("drive", {"speed_command": speed})


def stop():
    """두 모터 throttle = 0 → 브레이크 정지 (None은 관성 정지라서 쓰지 않는다)"""
    global current_speed
    # 초기화가 일부만 성공했어도 이미 만든 모터는 정지를 시도한다.
    try:
        if motor_left is not None:
            motor_left.throttle = 0
            motor_commands["M1"] = 0
            record_event("motor_stop", {"name": "M1"})
    finally:
        if motor_right is not None:
            motor_right.throttle = 0
            motor_commands["M2"] = 0
            record_event("motor_stop", {"name": "M2"})
    if current_speed != 0:
        current_speed = 0
        show_status("stop")
        record_event("stop")


def lock_wheels():
    """정지 후 팔 동작 직전에 호출. 이후 drive()는 무시된다."""
    global wheels_locked
    stop()
    wheels_locked = True
    show_status("lock")
    record_event("wheels_locked")


# =====================================================================
# 초음파
# =====================================================================
def get_distance():
    """앞쪽 거리(cm). 초음파 센서는 팔 베이스와 같이 돈다 → 주행 중엔 base가 정면이어야 한다"""
    global last_distance, distance_read_at, distance_error
    try:
        value = sonar.distance * 100
        if not math.isfinite(value) or value < 0:
            raise ValueError("유효하지 않은 초음파 거리")
        last_distance = value
        distance_read_at = time.monotonic()
        distance_error = None
    except Exception as exc:
        distance_error = f"{type(exc).__name__}: {exc}"
        raise
    return last_distance


# =====================================================================
# 카메라 / QR
# =====================================================================
def get_frame(fresh=False):
    """웹캠 프레임 1장(BGR 배열). 실패하면 None.
    fresh=True: 쌓여 있던 프레임을 버리고 새로 찍은 것을 받는다 (팔이 막 멈춘 직후, QR 대기 중에 사용)
    """
    if fresh:
        for _ in range(2):
            camera.grab()
    ok, frame = camera.read()
    return frame if ok else None


def read_qrs(frame, budget_s=0.25, expect_px=None):
    """frame 안의 QR들을 [(문자열, 중심x좌표), ...]로 돌려준다. 없으면 [].
    흐리거나 대비가 낮아도 읽도록 여러 방법을 시도한다 (qr_read.py). budget_s = 한 프레임에 쓸 최대 시간
    expect_px를 주면 화면 가운데를 그 크기에 맞게 확대해 먼저 읽는다 (expected_qr_px)"""
    return qr_read.read_qrs(frame, budget_s, expect_px)


def expected_qr_px(distance_cm):
    """출발 QR이 distance_cm 앞에 있을 때 웹캠 화면에서 한 변이 몇 픽셀로 찍힐지 (핀홀 카메라 계산).
    거리가 멀수록 작게 찍힌다: 픽셀 = 초점거리(px) × QR 크기(cm) / 거리(cm).
    camera.hfov_deg(가로 화각)는 python3 hw.py qr 로 잰다. 거리를 모르면 None"""
    if not distance_cm or distance_cm <= 0:
        return None
    c = cfg["camera"]
    focal = ((frame_width or c["width"]) / 2) / math.tan(math.radians(c["hfov_deg"] / 2))
    return focal * cfg["start_qr"]["size_cm"] / distance_cm


# =====================================================================
# 신호
# =====================================================================
def beep():
    """완료 신호: 짧게 두 번. 소리가 안 나도 미션 결과에는 영향을 주지 않는다"""
    if buzzer is None:
        return
    try:
        for _ in range(2):
            buzzer.play("C6")
            time.sleep(0.12)
            buzzer.stop()
            time.sleep(0.06)
    except Exception as exc:
        print(f"[BEEP] 완료음 실패 (무시): {type(exc).__name__}: {exc}", flush=True)


# =====================================================================
# 단독 테스트 — python3 hw.py <servo|motor|dist|qr>
# =====================================================================
def _servo_tool():
    print("서보 이름: steer base arm wrist grip")
    print("  base 120   → base를 120도로")
    print("  p          → 현재 팔 자세를 config.json에 붙여넣을 형태로 출력")
    print("  q          → 종료")
    while True:
        line = input("> ").strip()
        if line == "q":
            break
        if line == "p":
            pose = {n: round(servo_angles[n], 1) for n in ("base", "arm", "wrist") if n in servo_angles}
            print(json.dumps(pose))
            continue
        try:
            name, angle = line.split()
            if name not in servos:
                raise ValueError
            set_servo(name, float(angle))
        except ValueError:
            print("형식: 이름 각도   예) arm 60")


if __name__ == "__main__":
    test = sys.argv[1] if len(sys.argv) > 1 else ""
    if test not in ("servo", "motor", "dist", "qr"):
        print(__doc__)
        sys.exit(0)
    outcome, error = "completed", None
    try:
        init(use_camera=(test == "qr"), recording_mode=f"manual_{test}")
        set_phase(f"manual_{test}")
        if test == "servo":
            _servo_tool()
        elif test == "motor":
            spd = float(sys.argv[2]) if len(sys.argv) > 2 else cfg["motor"]["slow_speed"]
            sec = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
            get_distance()
            show_status("before")
            drive(spd)
            time.sleep(sec)
            stop()
            time.sleep(0.5)
            get_distance()
            show_status("after")
        elif test == "dist":
            while True:
                get_distance()
                show_status("dist")
                time.sleep(0.1)
        elif test == "qr":
            # 1초마다 웹캠 화면을 qr_view.jpg로 저장한다 (VS Code에서 열어 QR이 어떻게 보이는지 확인)
            snap = os.path.join(os.path.dirname(CONFIG_PATH), "qr_view.jpg")
            last_snap = 0.0
            print(f"zbar 사용: {'예' if qr_read.pyzbar else '아니오 (sudo apt install libzbar0 && pip install pyzbar)'}")
            while True:
                frame = get_frame(fresh=True)
                if frame is None:
                    print("프레임 없음 — 카메라 연결 확인", flush=True)
                    continue
                t = time.monotonic()
                qrs = qr_read.read_qrs_detail(frame, expect_px=expected_qr_px(last_distance or get_distance()))
                took = (time.monotonic() - t) * 1000
                print(f"[{elapsed():6.2f}s] {frame.shape[1]}x{frame.shape[0]} {took:4.0f}ms",
                      [(q[0], round(q[1])) for q in qrs] or "-", flush=True)
                if qrs:
                    # QR이 찍힌 크기와 초음파 거리로 웹캠 가로 화각을 거꾸로 계산 (config camera.hfov_deg에 넣는다)
                    width_px, d = qrs[0][2], get_distance()
                    focal = width_px * d / cfg["start_qr"]["size_cm"]
                    hfov = math.degrees(2 * math.atan((frame.shape[1] / 2) / focal))
                    print(f"           QR 한 변 {width_px:.0f}px · 거리 {d:.1f}cm → 가로 화각 약 {hfov:.0f}° "
                          f"(설정 {cfg['camera']['hfov_deg']}°, 예상 크기 {expected_qr_px(d):.0f}px)", flush=True)
                if qrs:
                    record_event("qr", {"qrs": qrs})
                if time.monotonic() - last_snap > 1:
                    cv2.imwrite(snap, frame)
                    last_snap = time.monotonic()
    except KeyboardInterrupt:
        outcome = "interrupted"
    except Exception as exc:
        outcome, error = "error", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        close(status=outcome, error=error)
