"""
drive_centered.py — drive.py(미션 1~2)에 직진 보정을 더한 버전.
바퀴 차이·진동으로 차가 옆으로 틀어지는 것을 자이로(MPU6050)로 잡는다.

  1) QR을 기다리는 동안(로봇 정지) 자이로 영점을 잰다
  2) 주행 중 방향(자이로 적분)과 옆으로 밀린 거리(방향 × 초음파로 잰 이동 거리)를 추정해
     조향 서보로 경기장 가운데 줄로 되돌린다
  3) 정지 후 방향·좌우 오차 추정값을 기록한다 (팔로 집을 때 보정에 쓸 값)
  바닥 센서 3개(좌·중·우)는 주행 중 값이 바뀐 지점을 기록한다 (테이프를 구별하는지 확인용)

실행 (Pi에서, 웹 조종 서비스를 끈 뒤 이 폴더 안에서)
    python3 drive_centered.py          # 주행
    python3 drive_centered.py gyro     # 방향 확인: 손으로 로봇을 왼쪽(반시계)으로 돌리면 + 가 커져야 한다
    python3 drive_centered.py line     # 바닥 센서 값 확인: 흰 종이 / 빨강·노랑 테이프 / 검은 선 위에서 비교

처음 쓸 때 맞출 값 (config.json "centering")
- gyro.sign: gyro 모드에서 왼쪽으로 돌렸는데 - 가 커지면 -1
- steer_sign: python3 hw.py servo 에서 "steer 100" 입력 시 바퀴가 왼쪽으로 꺾이면 1, 오른쪽이면 -1
- heading_gain(조향 서보 °/방향 오차 °), lateral_gain_deg_per_cm: 주행해 보며 조정. 0이면 보정을 끈다
필요한 라이브러리: pip install mpu6050-raspberrypi (Adeept 예제 Mpu6050.py와 같은 것)
"""
import math
import sys
import threading
import time

import hw
import mission
from drive import judge


class Gyro:
    """MPU6050 z축 회전 속도를 백그라운드에서 적분해 방향(°, 왼쪽 +)을 낸다.
    start_tracking() 전까지는 영점(정지 상태의 평균 회전 속도)만 모은다."""

    def __init__(self, cfg):
        try:
            from mpu6050 import mpu6050
        except ImportError as exc:
            raise RuntimeError("mpu6050 라이브러리가 없습니다: pip install mpu6050-raspberrypi") from exc
        self.sensor = mpu6050(cfg["address"])
        self.axis, self.sign = cfg["axis"], cfg["sign"]
        self.min_samples = cfg["min_calib_samples"]
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.bias_sum, self.bias_n, self.bias = 0.0, 0, None
        self.heading = 0.0
        self.errors = 0
        self.thread = threading.Thread(target=self._run, name="gyro", daemon=True)
        self.thread.start()

    def _rate(self):
        return self.sign * self.sensor.get_gyro_data()[self.axis]   # °/s

    def _run(self):
        last = time.monotonic()
        while not self.done.is_set():
            try:
                rate = self._rate()
            except OSError:
                self.errors += 1   # I2C 읽기 실패: 이번 표본만 건너뛴다
                time.sleep(0.005)
                continue
            now = time.monotonic()
            with self.lock:
                if self.bias is None:
                    self.bias_sum += rate
                    self.bias_n += 1
                else:
                    self.heading += (rate - self.bias) * (now - last)
            last = now
            time.sleep(0.005)

    def start_tracking(self):
        """지금을 방향 0°로 하고 적분을 시작한다. 영점 표본이 부족하면 오류"""
        with self.lock:
            if self.bias_n < self.min_samples:
                raise RuntimeError(f"자이로 영점 표본 부족 ({self.bias_n}개) — 연결과 I2C 주소 확인")
            self.bias = self.bias_sum / self.bias_n
            self.heading = 0.0
        return self.bias

    def read(self):
        with self.lock:
            return self.heading

    def close(self):
        self.done.set()
        self.thread.join(timeout=0.2)


class LineSensors:
    """바닥 적외선 센서 3개 (Adeept Functions.py와 같은 핀, gpiozero InputDevice)"""

    def __init__(self, pins):
        from gpiozero import InputDevice
        self.devices = {name: InputDevice(pin=pin) for name, pin in pins.items()}

    def read(self):
        return {name: d.value for name, d in self.devices.items()}

    def close(self):
        for d in self.devices.values():
            d.close()


def steer_for(heading, lateral):
    """방향(°, 왼쪽 +)과 좌우 위치(cm, 왼쪽 +)로 조향 서보 각도를 정한다.
    옆으로 밀렸으면 가운데 줄 쪽을 향하도록 목표 방향을 기울이고, 그 방향과의 차이만큼 꺾는다."""
    c, cc = hw.cfg, hw.cfg["centering"]
    limit = cc["max_heading_fix_deg"]
    want = max(-limit, min(limit, -cc["lateral_gain_deg_per_cm"] * lateral))
    turn = cc["heading_gain"] * (heading - want)               # + 면 오른쪽으로 꺾어야 한다
    turn = max(-cc["steer_limit_deg"], min(cc["steer_limit_deg"], turn))
    return c["steer_center"] - cc["steer_sign"] * turn


def drive_and_stop_centered(gyro, lines):
    """mission.drive_and_stop()과 같은 속도·정지 규칙으로 가되, 주행 중 조향으로 가운데 줄을 따른다.
    돌려주는 값: (정지 후 방향 °, 좌우 위치 추정 cm)"""
    c = hw.cfg
    st, m = c["stop"], c["motor"]
    brake_at = st["target_cm"] + st["brake_margin_cm"]
    hw.set_phase("drive")
    if mission.pose_ready("drive"):
        hw.move_servos(mission.pose("drive"))
    hw.set_servo("steer", c["steer_center"])

    d0 = d_prev = hw.get_distance()
    filt = mission.ApproachFilter(d0)
    lateral = 0.0
    line_prev = lines.read() if lines else None
    t_start = last_print = time.time()
    while True:
        d = filt.update(hw.get_distance())
        if d <= brake_at:
            hw.stop()
            break
        heading = gyro.read()
        # 초음파는 모니터까지 직선거리라 줄어든 만큼이 앞으로 간 거리. 튀는 값은 버린다
        step = d_prev - d
        if 0 < step < 3:
            lateral += math.sin(math.radians(heading)) * step
            d_prev = d
        elif step <= 0:
            d_prev = min(d_prev, d + 0.5)
        steer = steer_for(heading, lateral)
        if abs(steer - hw.servo_angles["steer"]) >= 0.5:   # 작은 변화는 무시 (서보 떨림·기록량 줄이기)
            hw.set_servo("steer", steer, show=False)
        hw.drive(m["max_speed"] if d > st["slow_down_cm"] else m["slow_speed"])
        if lines:
            now = lines.read()
            if now != line_prev:
                hw.record_event("line_change", {"distance_cm": d, "progress_cm": d0 - d, "values": now})
                line_prev = now
        if time.time() - last_print >= 0.1:
            hw.show_status("drive")
            print(f"           방향 {heading:+5.1f}°  좌우 {lateral:+5.1f}cm  조향 {hw.servo_angles['steer']:5.1f}",
                  flush=True)
            hw.record_event("centering", {"heading_deg": heading, "lateral_cm": lateral,
                                          "steer_deg": hw.servo_angles["steer"]})
            last_print = time.time()
        if time.time() - t_start > m["max_drive_s"]:
            hw.stop()
            raise RuntimeError("주행 시간 초과 — 초음파 값 확인 필요")
        time.sleep(0.01)
    mission.log(f"브레이크 (거리 {d:.1f}cm, 방향 {gyro.read():+.1f}°, 좌우 {lateral:+.1f}cm)")
    hw.record_event("brake", {"distance_cm": d})

    # 미세 보정은 앞뒤로만 짧게 움직이므로 조향은 가운데
    hw.set_servo("steer", c["steer_center"], show=False)
    mission.fine_adjust()
    return gyro.read(), lateral


def main():
    outcome, error = "completed", None
    gyro = lines = None
    try:
        hw.init(recording_mode="drive_centered")
        cc = hw.cfg["centering"]
        gyro = Gyro(cc["gyro"])                       # 지금부터 QR을 기다리는 동안 영점을 모은다
        try:
            lines = LineSensors(cc["line_pins"])
        except Exception as exc:                      # 바닥 센서는 기록용이라 없어도 주행한다
            mission.log(f"바닥 센서 없이 진행 ({type(exc).__name__}: {exc})")
        hw.set_phase("wait_start_qr")
        if mission.pose_ready("qr_view"):
            hw.move_servos(mission.pose("qr_view"))
        mission.log(f"출발 위치 거리 {hw.get_distance():.1f}cm (출발선이면 모니터까지 약 87cm)")
        mission.log("QR 대기 (이 동안 로봇을 건드리지 마세요: 자이로 영점 측정 중)")

        first = mission.wait_start_qr()
        bias = gyro.start_tracking()
        hw.record_event("start_qr", {"direction": first, "gyro_bias_dps": bias})
        hw.reset_clock()
        mission.log(f"출발: {first} (자이로 영점 {bias:+.2f}°/s)")

        heading, lateral = drive_and_stop_centered(gyro, lines)
        hw.lock_wheels()

        target = hw.cfg["stop"]["target_cm"]
        distance = hw.get_distance()
        result = judge(distance, target)
        hw.record_event("stop_judged", {"distance_cm": distance, "target_cm": target, "result": result,
                                        "heading_deg": heading, "lateral_cm": lateral})
        mission.log(f"예상 판정: {result} (거리 {distance:.1f}cm, 목표 {target}cm)")
        mission.log(f"정지 후 방향 {heading:+.1f}° · 좌우 {lateral:+.1f}cm (추정, 왼쪽 +)")
        hw.beep()
        hw.set_phase("done")
    except KeyboardInterrupt:
        outcome = "interrupted"
        mission.log("사용자 중단")
    except Exception as exc:
        outcome, error = "error", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            hw.close(status=outcome, error=error)
        finally:
            if gyro:
                gyro.close()
            if lines:
                lines.close()


def gyro_test():
    """2초 영점 후 방향을 계속 출력한다. 손으로 왼쪽(반시계)으로 돌리면 +가 커져야 한다."""
    cfg = hw.load_config()["centering"]["gyro"]
    gyro = Gyro(cfg)
    print("영점 측정 중 (2초, 로봇을 움직이지 마세요)")
    time.sleep(2)
    print(f"영점 {gyro.start_tracking():+.2f}°/s. Ctrl+C로 종료")
    try:
        while True:
            print(f"방향 {gyro.read():+7.1f}°", flush=True)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        gyro.close()


def line_test():
    """바닥 센서 값을 계속 출력한다. 흰 종이, 빨강·노랑 테이프, 검은 선 위에서 값이 다른지 본다."""
    lines = LineSensors(hw.load_config()["centering"]["line_pins"])
    print("left middle right. Ctrl+C로 종료")
    try:
        while True:
            v = lines.read()
            print(f"  {v['left']}     {v['middle']}      {v['right']}", flush=True)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        lines.close()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "gyro":
        gyro_test()
    elif mode == "line":
        line_test()
    elif mode:
        print(__doc__)
    else:
        main()
