"""
run_all.py — 미션 전체를 명령 하나로: 출발 QR → 직진(자이로 보정) → 정지선 정지 → 스펀지 두 개를 반대편으로.

실행 (Pi에서, 웹 조종 서비스를 끈 뒤 이 폴더 안에서)
    python3 run_all.py              # 전체 미션 (로봇을 출발선에 놓고)
    python3 run_all.py arm left     # 팔만: 지금 자리(정지선 위)에서 바퀴를 잠그고 LEFT 순서로 옮기기
    python3 run_all.py arm right    # 팔만: RIGHT 순서
    python3 run_all.py --yes        # 시작 전 확인(Enter) 없이 바로

옮기는 순서 (mission.py와 같음, 대회 배치 고정: 왼쪽 물체 뒤 칸, 오른쪽 물체 앞 칸)
  1) QR 쪽 물체를 집어 반대쪽 빈 칸에 놓는다  2) 반대쪽 물체를 집어 처음 쪽 빈 칸에 놓는다
스펀지를 들고 있을 때는 시뮬레이터에서 걸림 없이 확인한 경로로 움직인다:
  위(_above) → 열기 → 내려감(_down) → 닫기 → 들어 올림(_carry) → 같은 높이로 base만 회전
  → 놓을 칸 위(_carry) → 내려놓기(_place) → 열기 → 위로 빠짐(_carry)

config.json
- poses: 처음 값은 시뮬레이터 추정치다. python3 hw.py servo로 실물에 맞춘 뒤 p로 받은 값을 넣는다
- grip.open / grip.close: 반드시 실측해서 넣는다 (비어 있으면 실행하지 않는다)
- run_all.wrist_flip: wrist 값을 올렸을 때 목이 내려가는 조립이면 true (모든 자세의 wrist를 180-값으로 바꿔 쓴다)
- run_all.use_gyro: 자이로 직진 보정 사용 (실패하면 보정 없이 주행)
- run_all.carry_move_s: 스펀지를 들고 들어 올리거나 위로 빠지는 한 동작의 시간
- run_all.swing_move_s: 스펀지를 든 채 반대쪽 칸 위로 회전하는 시간 (빠르면 흔들려 놓칠 수 있다)
- run_all.place_press: 놓는 자세(_place)에 더해 착지 높이를 정하는 양. 공중에서 떨어지면 arm·wrist를 같이 +3씩
- run_all.place_move_s: 들어 올린 높이에서 착지 높이까지 내려가는 시간 (짧을수록 빠르게)
- run_all.place_settle_s: 다 내려간 뒤 집게를 열기 전 기다리는 시간
- grip.release: 놓을 때 여는 각도. 완전히 열면(open) 스펀지를 튕길 수 있어 그보다 조금 덜 연다
- grip.release_wide / run_all.wide_release: 이 목록의 칸에 놓을 때는 release 대신 release_wide까지 연다
- run_all.release_hold_s: 놓을 때 연 채로 기다리는 시간. 스펀지가 집게에 붙어 딸려 오면 늘린다
- run_all.reach_adjust: 집기·놓기 자세 전체에 더할 값. 덜 뻗으면 arm을 +3~5씩, 집게가 아래로 꺾여 있으면 wrist를 +5~10씩
- wrist 값은 서보 90 = 목이 수평보다 약 10° 아래가 되도록 서보를 다시 끼운 기준이다
"""
import sys
import time

import hw
import mission
from drive import judge

FIXED_POSES = ["qr_view", "drive"]


def transfer_poses(cfg):
    """고정 배치에서 쓰는 자세 이름들 (집는 칸: above/down/carry, 놓는 칸: carry/place)"""
    place = cfg["look"]["default_place"]
    names = list(FIXED_POSES)
    for side in ("left", "right"):
        pick = "back" if place[side] == "front" else "front"
        names += [f"{side}_{pick}_above", f"{side}_{pick}_down", f"{side}_{pick}_carry",
                  f"{side}_{place[side]}_carry", f"{side}_{place[side]}_place"]
    return names


def check_config(cfg):
    """필요한 값만 검사한다. 비어 있는 항목 목록을 돌려준다."""
    missing = [f"grip.{k}" for k in ("open", "close", "release") if cfg["grip"].get(k) is None]
    for name in transfer_poses(cfg):
        p = cfg["poses"].get(name)
        if p is None or any(v is None for v in p.values()):
            missing.append(f"poses.{name}")
    return missing


def apply_wrist_flip(cfg):
    """wrist 방향이 반대인 조립이면 모든 자세의 wrist를 180-값으로 바꾼다 (메모리에서만)"""
    if cfg["run_all"]["wrist_flip"]:
        for p in cfg["poses"].values():
            if p.get("wrist") is not None:
                p["wrist"] = 180 - p["wrist"]


def apply_reach_adjust(cfg):
    """집기·놓기 자세에 같은 보정을 더한다 (메모리에서만). 현장에서 덜/더 뻗을 때 한 줄로 조정한다.
    arm +: 더 숙여 멀리·낮게 / wrist +: 집게가 수평에 가까워져 멀리·높게"""
    adj = cfg["run_all"]["reach_adjust"]
    for name in transfer_poses(cfg):
        if name in FIXED_POSES:
            continue
        p = cfg["poses"][name]
        for joint in ("arm", "wrist"):
            p[joint] = max(0, min(180, p[joint] + adj[joint]))


def show_plan(cfg):
    print("\n사용할 값 (wrist_flip 적용 후)")
    g, press = cfg["grip"], cfg["run_all"]["place_press"]
    print(f"  grip  open={g['open']}  close={g['close']}  release={g['release']}")
    print(f"  놓을 때 더 내림  arm +{press['arm']}  wrist +{press['wrist']}")
    for name in transfer_poses(cfg):
        p = cfg["poses"][name]
        print(f"  {name:<18} base={p['base']:>5}  arm={p['arm']:>5}  wrist={p['wrist']:>5}")
    print()


# =====================================================================
# 팔: 옮기기
# =====================================================================
def move(name, slow=False):
    hw.set_phase(name)
    hw.move_servos(mission.pose(name), hw.cfg["run_all"]["carry_move_s"] if slow else None)


def pick(side, slot):
    """위로 가면서 열기 → 내려감 → 닫기 → 들어 올림. 들어 올릴 때부터는 스펀지를 들고 있으니 천천히.
    집게를 위 자세로 가는 동작과 함께 열어, 위에서 열기를 기다리며 멈추는 시간을 없앤다."""
    name = f"{side}_{slot}_above"
    hw.set_phase(name)
    hw.move_servos({**mission.pose(name), "grip": hw.cfg["grip"]["open"]})
    move(f"{side}_{slot}_down")
    hw.grip("close")
    move(f"{side}_{slot}_carry", slow=True)
    mission.log(f"{side} {slot} 집기 완료")


def place(side, slot):
    """놓을 칸 위로 한 번에 회전 → 착지 높이까지 한 번에 빠르게 내림 → 살짝 열기 → 위로 빠짐.
    공중에서 집게를 열면 스펀지가 던져지듯 떨어지므로, 바닥(착지 높이)까지 쥔 채로 내려간 뒤
    움직임이 멎으면 조금만(release) 천천히 연다. 완전히 여는 것은 다음 집기 때 한다.
    회전: 들어 올린 자세끼리는 arm·wrist가 비슷해 높이가 거의 그대로이므로 base와 함께 swing_move_s 동안 움직인다.
    착지 높이 = 놓는 자세(_place) + place_press. 내려가는 시간 = place_move_s"""
    ra = hw.cfg["run_all"]
    hw.set_phase(f"{side}_{slot}_swing")
    hw.move_servos(mission.pose(f"{side}_{slot}_carry"), ra["swing_move_s"])
    land = dict(mission.pose(f"{side}_{slot}_place"))
    for joint in ("arm", "wrist"):
        land[joint] = max(0, min(180, land[joint] + ra["place_press"][joint]))
    hw.set_phase(f"{side}_{slot}_land")
    hw.move_servos(land, ra["place_move_s"])
    time.sleep(ra["place_settle_s"])          # 팔 흔들림이 멎은 뒤 연다
    wide = f"{side}_{slot}" in ra["wide_release"]   # 잘 안 놓이는 칸은 더 넓게 연다
    hw.grip("release_wide" if wide else "release")
    time.sleep(ra["release_hold_s"])          # 집게에 붙은 스펀지가 떨어질 때까지 연 채로 기다린다
    move(f"{side}_{slot}_carry", slow=True)
    mission.log(f"{side} {slot} 놓기 완료")


def move_objects(first):
    """mission.py main()의 집기·놓기 순서. look()은 고정 배치를 바로 돌려준다 (look.use_qr = false)"""
    second = mission.other_side(first)
    place_first = mission.look(first)
    pick(first, mission.other_slot(place_first))
    place_second = mission.look(second)
    place(second, place_second)
    pick(second, mission.other_slot(place_second))
    place(first, place_first)
    move("drive")   # 팔을 정리한 자세로


# =====================================================================
# 주행
# =====================================================================
def start_gyro():
    """자이로 보정 준비. 라이브러리나 센서가 없으면 None (보정 없이 주행)"""
    if not hw.cfg["run_all"]["use_gyro"]:
        return None, None
    import drive_centered
    try:
        gyro = drive_centered.Gyro(hw.cfg["centering"]["gyro"])
    except Exception as exc:
        mission.log(f"자이로 없이 주행 ({type(exc).__name__}: {exc})")
        return None, None
    try:
        lines = drive_centered.LineSensors(hw.cfg["centering"]["line_pins"])
    except Exception:
        lines = None
    return gyro, lines


def drive_to_line(gyro, lines):
    """정지선까지 간다. 돌려주는 값: 정지 후 차의 방향(°, 왼쪽 +). 자이로가 없으면 0"""
    if gyro is None:
        mission.drive_and_stop()
        return 0.0
    import drive_centered
    heading, lateral = drive_centered.drive_and_stop_centered(gyro, lines)
    mission.log(f"정지 후 방향 {heading:+.1f}° · 좌우 {lateral:+.1f}cm (추정, 왼쪽 +)")
    return heading


def apply_heading(cfg, heading):
    """차가 틀어진 만큼 집기·놓기 자세의 base를 반대로 돌린다 (상자는 경기장에 고정돼 있으므로).
    왼쪽(+)으로 틀어졌으면 base를 그만큼 줄인다. 너무 크면 자이로 값을 믿지 않는다"""
    limit = cfg["run_all"]["heading_fix_max_deg"]
    if not heading or abs(heading) > limit:
        if heading:
            mission.log(f"방향 {heading:+.1f}°가 너무 커서 팔 보정 안 함 (최대 {limit}°)")
        return
    for name in transfer_poses(cfg):
        if name not in FIXED_POSES:
            p = cfg["poses"][name]
            p["base"] = max(0, min(180, round(p["base"] - heading)))
    mission.log(f"차 방향 {heading:+.1f}°만큼 팔 base 보정")


# =====================================================================
# 전체 흐름
# =====================================================================
def main():
    args = [a for a in sys.argv[1:] if a != "--yes"]
    arm_only = bool(args) and args[0] == "arm"
    if arm_only and (len(args) < 2 or args[1] not in ("left", "right")):
        print("사용법: python3 run_all.py arm left  또는  python3 run_all.py arm right")
        return
    if args and not arm_only:
        print(__doc__)
        return

    cfg = hw.load_config()
    missing = check_config(cfg)
    if missing:
        print(f"비어 있는 값 {len(missing)}개 — config.json에 채운 뒤 다시 실행하세요:")
        for m in missing:
            print("  -", m)
        print("자세: python3 hw.py servo 에서 맞추고 p 로 값 받기 / 집게: grip 값을 바꿔 가며 열림·닫힘 각도 찾기")
        return
    apply_wrist_flip(cfg)
    apply_reach_adjust(cfg)
    show_plan(cfg)
    if cfg["run_all"]["confirm"] and "--yes" not in sys.argv:
        input("로봇 주변을 비우고 Enter를 누르면 시작합니다 (취소: Ctrl+C) ")

    outcome, error = "completed", None
    gyro = lines = None
    try:
        hw.init(use_camera=not arm_only, recording_mode="arm_only" if arm_only else "run_all")
        hw.cfg = cfg   # wrist_flip을 적용한 설정으로 이후 모든 동작을 한다
        if arm_only:
            first = args[1]
            hw.lock_wheels()
            mission.log(f"팔만 시험: {first} 순서 (바퀴 잠금)")
            move("drive")
        else:
            gyro, lines = start_gyro()   # QR을 기다리는 동안 자이로 영점을 모은다
            hw.set_phase("wait_start_qr")
            move("qr_view")
            if gyro:
                time.sleep(0.3)               # 팔 흔들림이 멎은 뒤부터 영점을 잰다
                gyro.restart_calibration()
            mission.log(f"출발 위치 거리 {hw.get_distance():.1f}cm (출발선이면 모니터까지 약 87cm)")
            mission.log("QR 대기" + (" (로봇을 건드리지 마세요: 자이로 영점 측정 중)" if gyro else ""))
            first = mission.wait_start_qr()
            if gyro:
                gyro.start_tracking()
            hw.record_event("start_qr", {"direction": first})
            hw.reset_clock()
            mission.log(f"출발: {first}")

            heading = drive_to_line(gyro, lines)
            hw.lock_wheels()                         # 이후 바퀴 명령 무시 (팔 동작 중 이동 = 실격)
            target = cfg["stop"]["target_cm"]
            distance = hw.get_distance()
            result = judge(distance, target)
            hw.record_event("stop_judged", {"distance_cm": distance, "target_cm": target, "result": result})
            mission.log(f"예상 판정: {result} (거리 {distance:.1f}cm)")
            apply_heading(cfg, heading)

        move_objects(first)
        hw.beep()
        hw.set_phase("done")
        mission.log("DONE — 조교 확인 후 종료 버튼")
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


if __name__ == "__main__":
    main()
