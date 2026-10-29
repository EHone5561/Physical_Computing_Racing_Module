"""
drive.py — 미션 1~2만: 출발 QR 인식 → 직진 → 모니터 30cm 앞 정지선에 정지.
팔로 물체를 옮기는 미션 3~4는 하지 않는다. 정지 후 바퀴를 잠근다.

실행 (Pi에서, 웹 조종 서비스를 끈 뒤 이 폴더 안에서): python3 drive.py

경기장 규격 (PiCar.pdf)
- 정지 판정은 초음파 센서 위치 기준. 정지선 중심 = 모니터 전면에서 30cm (config "stop.target_cm")
- 빨간 선 2cm 위 = 만점, 양쪽 노란 선 각 2cm 위 = 감점, 정지선을 넘어가면 실격
- 출발선 → 모니터 약 87cm (경기장 109cm - 출발선 22cm), 정지선까지는 약 57cm
- 출발 QR 문자열은 "Left" / "Right" (QR/Left.png, QR/Right.png). QR을 읽기 전에는 움직이지 않는다

config.json의 poses.qr_view / poses.drive가 null이면 팔은 지금 자세 그대로 둔다.
초음파가 팔 베이스와 같이 돌기 때문에 base가 정면인지 출발 전에 눈으로 확인한다.
"""
import hw
import mission

RED_HALF_CM = 1.0   # 빨간 선 폭 2cm의 절반
YELLOW_CM = 2.0     # 빨간 선 양쪽 노란 선 폭


def judge(distance, target):
    """정지 거리(cm)로 예상 판정을 돌려준다. 센서가 선 중심에서 벗어난 거리로 판단"""
    err = distance - target
    if abs(err) <= RED_HALF_CM:
        return "빨강(만점)"
    if abs(err) <= RED_HALF_CM + YELLOW_CM:
        return "노랑(감점)"
    return "정지선 넘음(실격)" if err < 0 else "정지선 못 미침"


def main():
    outcome, error = "completed", None
    try:
        hw.init(recording_mode="drive")
        hw.set_phase("wait_start_qr")
        if mission.pose_ready("qr_view"):
            hw.move_servos(mission.pose("qr_view"))    # 모니터 QR이 보이는 자세로 대기
        else:
            mission.log("poses.qr_view 미측정 → 팔은 현재 자세로 QR 대기")
        mission.log(f"출발 위치 거리 {hw.get_distance():.1f}cm (출발선이면 모니터까지 약 87cm)")
        mission.log("QR 대기")

        first = mission.wait_start_qr()                # "left" / "right"
        hw.record_event("start_qr", {"direction": first})
        hw.reset_clock()
        mission.log(f"출발: {first}")

        mission.drive_and_stop()
        hw.lock_wheels()                               # 이후 바퀴 명령 무시 (실격 방지)

        target = hw.cfg["stop"]["target_cm"]
        distance = hw.get_distance()
        result = judge(distance, target)
        hw.record_event("stop_judged", {"distance_cm": distance, "target_cm": target, "result": result})
        mission.log(f"예상 판정: {result} (거리 {distance:.1f}cm, 목표 {target}cm)")
        hw.beep()
        hw.set_phase("done")
    except KeyboardInterrupt:
        outcome = "interrupted"
        mission.log("사용자 중단")
    except Exception as exc:
        outcome, error = "error", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        hw.close(status=outcome, error=error)


if __name__ == "__main__":
    main()
