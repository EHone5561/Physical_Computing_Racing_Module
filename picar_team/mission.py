"""
mission.py — PiCar의 "머리": 미션 순서와 판단만 둔다.
하드웨어 세부(채널 번호 등)는 hw.py, 숫자는 config.json.

실행 (Pi에서): python3 mission.py
- config.json에 null(측정 안 한 값)이 남아 있으면 실행하지 않고 목록만 보여준다
- 터미널에 매 순간 속도·서보 각도·거리가 출력된다
"""
from collections import deque
import statistics
import time

import hw


def log(msg):
    print(f"[{hw.elapsed():6.2f}s] >>> {msg}", flush=True)


def other_side(side):
    return "right" if side == "left" else "left"


def other_slot(slot):
    return "back" if slot == "front" else "front"


def pose(name):
    """config.json의 poses[name] (예: "left_front_down")"""
    return hw.cfg["poses"][name]


def pose_ready(name):
    """poses[name]의 각도가 모두 측정되어 있으면 True"""
    return all(v is not None for v in pose(name).values())


def find_nulls(value, path="config"):
    """config에서 null(아직 측정 안 한 값)의 위치를 모두 찾는다."""
    if value is None:
        return [path]
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            found += find_nulls(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found += find_nulls(v, f"{path}[{i}]")
    return found


# =====================================================================
# 단계별 함수
# =====================================================================
def start_direction(qrs):
    """QR 목록에서 Left/Right를 찾아 "left"/"right"로. 없으면 None"""
    for text, _ in qrs:
        t = text.lower()
        if t in ("left", "right"):
            return t
    return None


def wait_start_qr():
    """시작 QR(Left/Right)을 읽을 때까지 기다렸다가 "left" 또는 "right"를 돌려준다.
    이 동안 바퀴는 움직이지 않는다.
    """
    sc = hw.cfg["start_qr"]

    # 이전 경기의 QR이 화면에 남아 있으면 바로 출발해 버린다 → 먼저 '빈 화면'을 확인
    if sc["require_blank_first"]:
        blank, warned = 0, False
        while blank < sc["blank_frames"]:
            frame = hw.get_frame()
            if frame is None:
                continue
            if start_direction(hw.read_qrs(frame)) is None:
                blank += 1
            else:
                blank = 0
                if not warned:
                    log("이전 QR이 화면에 남아 있음 → 사라질 때까지 대기")
                    warned = True
        log("화면 비어 있음 확인 → 시작 QR 대기")

    # 같은 값이 confirm_frames 프레임 연속 나와야 확정 (오인식 방지)
    last, count = None, 0
    while True:
        frame = hw.get_frame()
        if frame is None:
            continue
        d = start_direction(hw.read_qrs(frame))
        if d is None:
            last, count = None, 0
            continue
        count = count + 1 if d == last else 1
        last = d
        if count >= sc["confirm_frames"]:
            return d


class ApproachFilter:
    """앞으로 다가가는 동안 초음파 값이 튀는 것을 거른다 (진동·반사로 51cm 다음에 71cm 같은 값이 나온다).
    - 앞으로만 가므로 직전 값보다 jump_cm 넘게 멀어진 값은 버린다. 3번 연속이면 진짜로 보고 받아들인다
    - 감속·브레이크 판단은 최근 window개 중 가장 가까운 값으로 한다 (늦게 서는 쪽보다 일찍 서는 쪽이 안전)"""

    def __init__(self, first):
        f = hw.cfg["sonar_filter"]
        self.jump, self.last, self.rejects = f["jump_cm"], first, 0
        self.recent = deque([first], maxlen=f["window"])

    def update(self, d):
        if d > self.last + self.jump and self.rejects < 2:
            self.rejects += 1
        else:
            self.last, self.rejects = d, 0
            self.recent.append(d)
        return min(self.recent)


def steady_distance(n=5, gap_s=0.03):
    """멈춘 상태에서 여러 번 재서 중앙값 (튀는 값 하나에 흔들리지 않게)"""
    values = []
    for _ in range(n):
        values.append(hw.get_distance())
        time.sleep(gap_s)
    return statistics.median(values)


def drive_and_stop():
    """정지선까지 가서 멈춘다. 끝나면 바퀴는 정지 상태."""
    c = hw.cfg
    hw.set_phase("drive")
    st, m = c["stop"], c["motor"]
    target = st["target_cm"]
    brake_at = target + st["brake_margin_cm"]   # 제동 거리만큼 일찍 브레이크

    # 1) 팔을 주행 자세로 (base 정면: 초음파가 같이 돈다 / 팔이 모니터에 닿지 않게)
    #    아직 측정 전(null)이면 팔은 지금 자세 그대로 둔다 (drive.py 시험 주행용)
    if pose_ready("drive"):
        hw.move_servos(pose("drive"))
    else:
        log("poses.drive 미측정 → 팔은 현재 자세로 주행")
    hw.set_servo("steer", c["steer_center"])

    # 2) 접근: 멀면 빠르게, 가까우면 느리게, 제동 거리에 오면 브레이크
    t_start = time.time()
    last_print = 0.0
    filt = ApproachFilter(hw.get_distance())
    while True:
        d = filt.update(hw.get_distance())
        if d <= brake_at:
            hw.stop()
            break
        hw.drive(m["max_speed"] if d > st["slow_down_cm"] else m["slow_speed"])
        if time.time() - last_print >= 0.1:          # 0.1초마다 속도·거리 출력
            hw.show_status("drive")
            last_print = time.time()
        if time.time() - t_start > m["max_drive_s"]:
            hw.stop()
            raise RuntimeError("주행 시간 초과 — 초음파 값 확인 필요")
        time.sleep(0.01)
    log(f"브레이크 (거리 {d:.1f}cm)")
    hw.record_event("brake", {"distance_cm": d})
    fine_adjust()


def fine_adjust():
    """미세 보정: 멈춘 뒤 다시 재서 허용 오차 밖이면 아주 짧게 전진/후진
    (팔이 움직이기 전이라 규칙상 허용). drive_centered.py도 같이 쓴다."""
    st, m = hw.cfg["stop"], hw.cfg["motor"]
    target = st["target_cm"]
    hw.set_phase("fine_adjust")
    d = hw.last_distance
    for _ in range(st["fine_max_tries"]):
        time.sleep(st["settle_s"])
        d = steady_distance()
        err = d - target
        hw.show_status("fine")
        if abs(err) <= st["tolerance_cm"]:
            break
        hw.drive(m["fine_speed"] if err > 0 else -m["fine_speed"])
        time.sleep(m["fine_pulse_s"])
        hw.stop()
    else:
        time.sleep(st["settle_s"])   # 마지막 보정 뒤의 위치를 다시 잰다
        d = steady_distance()
    log(f"정지 완료 (거리 {d:.1f}cm, 목표 {target}cm)")


def look(side):
    """side쪽 보기 자세로 팔을 돌려, 그 상자의 PLACE가 "front"(모니터 쪽)인지 "back"인지 돌려준다.
    QR 하나만 읽혀도 판단한다:
      - PLACE, PICK 둘 다 보이면 → 둘의 x좌표 비교 (가장 확실)
      - PLACE만 보이면           → 화면 중앙과 비교
      - PICK만 보이면            → PLACE는 그 반대쪽
    config "look.use_qr"가 false면 팔을 돌리지 않고 default_place(대회 고정 배치)를 그대로 쓴다.
    """
    lk = hw.cfg["look"]
    if not lk.get("use_qr", True):
        slot = lk["default_place"][side]
        log(f"{side} 상자: PLACE = {slot} (배치 고정, QR 읽지 않음)")
        hw.record_event("place_fixed", {"side": side, "slot": slot})
        return slot
    hw.set_phase(f"look_{side}")
    hw.move_servos(pose(f"look_{side}"))
    time.sleep(lk["settle_s"])

    for attempt in range(1, lk["retries"] + 1):
        frame = hw.get_frame(fresh=True)
        if frame is None:
            continue
        center = frame.shape[1] / 2
        qrs = hw.read_qrs(frame)
        hw.record_event("box_qr", {"side": side, "qrs": qrs})
        place_x = [x for t, x in qrs if t.upper() == "PLACE"]
        pick_x = [x for t, x in qrs if t.upper() == "PICK"]

        if place_x and pick_x:
            place_on_left = place_x[0] < pick_x[0]
            how = "PLACE+PICK"
        elif place_x:
            place_on_left = place_x[0] < center
            how = "PLACE"
        elif pick_x:
            place_on_left = not (pick_x[0] < center)
            how = "PICK"
        else:
            log(f"{side} QR 못 찾음 ({attempt}/{lk['retries']})")
            continue

        front_on_left = lk["front_on_image_left"][side]
        slot = "front" if place_on_left == front_on_left else "back"
        log(f"{side} 상자: PLACE = {slot} ({how}로 판단)")
        hw.record_event("place_identified", {"side": side, "slot": slot, "method": how})
        return slot

    slot = lk["default_place"][side]
    log(f"{side} 판별 실패 → 기본값 PLACE = {slot}")
    hw.record_event("place_fallback", {"side": side, "slot": slot})
    return slot


def pick(side, slot):
    """side/slot 칸의 물체를 집는다: 위 → 집게 열기 → 아래 → 집게 닫기 → 위"""
    hw.set_phase(f"pick_{side}_{slot}")
    hw.move_servos(pose(f"{side}_{slot}_above"))
    hw.grip("open")
    hw.move_servos(pose(f"{side}_{slot}_down"))
    hw.grip("close")
    hw.move_servos(pose(f"{side}_{slot}_above"))


def place(side, slot):
    """side/slot 칸의 놓기 자세로 가서 집게를 연다 (상자 바로 위에서 놓기)"""
    hw.set_phase(f"place_{side}_{slot}")
    hw.move_servos(pose(f"{side}_{slot}_place"))
    hw.grip("open")


# =====================================================================
# 전체 흐름
# =====================================================================
def main():
    cfg = hw.load_config()
    missing = find_nulls(cfg)
    if not cfg.get("look", {}).get("use_qr", True):
        # 배치 고정이면 상자를 보는 자세(look_left/right)는 쓰지 않는다
        missing = [p for p in missing if not p.startswith(("config.poses.look_left", "config.poses.look_right"))]
    if missing:
        print(f"측정 안 한 값(null)이 {len(missing)}개 있어 실행하지 않습니다:")
        for p in missing:
            print("  -", p)
        print("python3 hw.py servo 로 각도를 재서 config.json에 채운 뒤 다시 실행하세요.")
        return

    outcome, error = "completed", None
    try:
        hw.init(recording_mode="mission")
        hw.set_phase("wait_start_qr")
        hw.move_servos(pose("qr_view"))          # 모니터 QR이 보이는 자세로 대기
        log("QR 대기")

        first = wait_start_qr()                  # "left" / "right"
        hw.record_event("start_qr", {"direction": first})
        hw.reset_clock()
        log(f"출발: {first}")

        drive_and_stop()
        hw.lock_wheels()                         # 이후 바퀴 명령 무시 (실격 방지)
        second = other_side(first)

        # 1~2) 시작 QR 쪽을 보고 PLACE 위치를 기억한 뒤, PICK의 물체를 집는다
        place_first = look(first)
        pick(first, other_slot(place_first))
        log("1번 집기 완료")

        # 3) 물체를 든 채로 반대쪽을 보고 PLACE 위치를 기억한다
        place_second = look(second)

        # 4) 반대쪽 PLACE에 첫 물체를 놓는다
        place(second, place_second)
        log("1번 놓기 완료")

        # 5) 반대쪽 PICK의 물체를 집어, 처음 기억한 PLACE에 놓는다
        pick(second, other_slot(place_second))
        log("2번 집기 완료")
        place(first, place_first)
        log("2번 놓기 완료")

        hw.beep()
        hw.set_phase("done")
        log("DONE")
    except KeyboardInterrupt:
        outcome = "interrupted"
        log("사용자 중단")
    except Exception as exc:
        outcome, error = "error", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        hw.close(status=outcome, error=error)


if __name__ == "__main__":
    main()
