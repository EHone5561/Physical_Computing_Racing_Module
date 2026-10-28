"""하드웨어와 독립적인 시연 JSON 기록기. web_monitor에도 같은 파일을 배포한다.

웹 시연 제어 (실제 Server 폴더에서):
    python3 practice_logger.py start --label left-test
    python3 practice_logger.py stop
결과 추가 (기록을 종료한 파일에만):
    python3 practice_logger.py annotate practices/practice1.json --success yes --notes test
"""
import argparse
import copy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid


DEFAULTS = {"enabled": True, "directory": "practices", "interval_s": 0.1, "flush_s": 1.0}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def settings(base, overrides=None):
    result = dict(DEFAULTS)
    result.update(overrides or {})
    for key in ("interval_s", "flush_s"):
        value = float(result[key])
        if not math.isfinite(value) or value < 0.02:
            raise ValueError(f"recording.{key}는 0.02초 이상의 유한한 수여야 합니다")
        result[key] = value
    directory = Path(result["directory"])
    result["directory"] = directory if directory.is_absolute() else Path(base) / directory
    return result


def _clean(value):
    """표준 JSON에서 허용하지 않는 NaN/Infinity는 미확인 값(null)으로 남긴다."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_clean(v) for v in value]
    return value


def atomic_json(path, data):
    """완성된 임시 파일로 교체해 저장 도중 기존 JSON이 잘리는 것을 피한다."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".practice-", suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            json.dump(_clean(data), f, ensure_ascii=False, allow_nan=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class PracticeRecorder:
    """주기 표본 + 명령 순간 표본. 파일 쓰기는 별도 스레드에서 한다."""

    def __init__(self, directory, mode, snapshot, config=None, label="",
                 interval_s=0.1, flush_s=1.0):
        validated = settings(directory, {"directory": str(Path(directory).resolve()),
                                        "interval_s": interval_s, "flush_s": flush_s})
        self.directory = validated["directory"]
        self.directory.mkdir(parents=True, exist_ok=True)
        self.snapshot = snapshot
        self.interval = validated["interval_s"]
        self.flush_interval = validated["flush_s"]
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.started = time.monotonic()
        self.closed = False
        self.last_error = None
        self.data = {
            "schema_version": 1, "mode": mode, "label": label,
            "started_at": utc_now(), "ended_at": None, "run_status": "running",
            "sample_interval_s": self.interval,
            "config_snapshot": _clean(copy.deepcopy(config)),
            "value_meaning": {
                "servo_deg": "마지막 성공한 서보 명령 각도. 실제 각도 측정 아님",
                "motor_throttle": "모터 명령 -1~1. 실제 속도/RPM 아님",
                "distance_cm": "초음파 라이브러리 반환값. 새 에코 수신 보장 아님",
                "distance_read_age_s": "프로그램이 거리 값을 읽은 뒤 지난 시간",
                "elapsed_s": "기록 시작 이후 단조 증가 시간. 출발 QR에서 초기화하지 않음",
                "null": "아직 알 수 없거나 관찰하지 않은 값",
            },
            "result": {"success": None, "stop_error_cm": None, "notes": ""},
            "samples": [],
        }
        numbers = [int(m.group(1)) for p in self.directory.glob("practice*.json")
                   if (m := re.fullmatch(r"practice(\d+)\.json", p.name))]
        number = max(numbers, default=0) + 1
        while True:
            self.path = self.directory / f"practice{number}.json"
            try:
                # 배타적 생성: 동시에 시작해도 기존 시연 파일을 덮어쓰지 않는다.
                with self.path.open("x", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, allow_nan=False)
                break
            except FileExistsError:
                number += 1
        self.record("start", kind="event")
        self.thread = threading.Thread(target=self._run, name="practice-json", daemon=True)
        self.thread.start()
        print(f"[RECORD] 시작: {self.path}", flush=True)

    def record(self, event=None, details=None, kind="command"):
        """하드웨어 코드에서 호출: 메모리에 추가하고 즉시 반환한다."""
        with self.lock:
            if self.closed:
                return
            try:
                state = _clean(self.snapshot())
            except Exception as exc:
                state = {"snapshot_error": type(exc).__name__ + ": " + str(exc)}
            self.data["samples"].append({
                "seq": len(self.data["samples"]),
                "elapsed_s": round(time.monotonic() - self.started, 6),
                "timestamp": utc_now(), "kind": kind, "event": event,
                "details": _clean(copy.deepcopy(details)), **copy.deepcopy(state),
            })

    def save(self):
        with self.lock:
            data = copy.deepcopy(self.data)
        try:
            atomic_json(self.path, data)
            self.last_error = None
            return True
        except (OSError, TypeError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            if self.last_error != error:
                print(f"[RECORD ERROR] 저장 실패 {self.path}: {error}", flush=True)
            self.last_error = error
            return False

    def _run(self):
        next_save = time.monotonic() + self.flush_interval
        while not self.done.wait(self.interval):
            self.record(kind="periodic")
            if time.monotonic() >= next_save:
                self.save()
                next_save = time.monotonic() + self.flush_interval

    def close(self, status="ended", error=None, result=None):
        if self.closed:
            return
        self.done.set()
        self.thread.join()
        self.record("end", {"status": status, "error": error}, kind="event")
        with self.lock:
            self.closed = True
            self.data["ended_at"] = utc_now()
            self.data["duration_s"] = round(time.monotonic() - self.started, 6)
            self.data["run_status"] = status
            self.data["error"] = error
            if result:
                self.data["result"].update(result)
        ok = self.save()
        print(f"[RECORD] {'저장 완료' if ok else '저장 실패'}: {self.path}", flush=True)


class WebRecordingControl:
    """SSH 요청 파일을 받아 기록만 시작/종료한다. 로봇 제어는 하지 않는다."""

    def __init__(self, base, snapshot, overrides=None, config=None):
        self.options = settings(base, overrides)
        self.directory = self.options["directory"]
        self.directory.mkdir(parents=True, exist_ok=True)
        self.request_path = self.directory / ".record-request.json"
        self.snapshot = snapshot
        self.config = config
        self.recorder = None
        self.control_lock = threading.RLock()
        self.closed = False
        self.last_request_id = None
        # 서버 시작 전의 오래된 명령은 실행하지 않는다.
        if self.request_path.exists():
            try:
                self.last_request_id = json.loads(self.request_path.read_text(encoding="utf-8"))["id"]
            except (OSError, ValueError, KeyError):
                pass

    def record(self, event, details=None):
        recorder = self.recorder
        if recorder is not None:
            recorder.record(event, details)

    def poll(self):
        with self.control_lock:
            if not self.closed:
                self._poll()

    def _poll(self):
        if not self.request_path.exists():
            return
        try:
            request = json.loads(self.request_path.read_text(encoding="utf-8"))
            request_id = request["id"]
            if request_id == self.last_request_id:
                return
            self.last_request_id = request_id
            response = {"id": request_id, "ok": False}
            if not 0 <= time.time() - request["created_at"] <= 10:
                raise ValueError("유효 시간이 지난 요청입니다. 다시 요청하세요")
            action = request["action"]
            if action == "start":
                if self.recorder is not None:
                    raise ValueError("이미 기록 중입니다. stop 후 다음 시연을 start 하세요")
                self.recorder = PracticeRecorder(
                    self.directory, "web_manual", self.snapshot, self.config,
                    request.get("label", ""), self.options["interval_s"], self.options["flush_s"])
                response["path"] = str(self.recorder.path)
            elif action == "stop":
                if self.recorder is None:
                    raise ValueError("기록 중인 시연이 없습니다")
                recorder, self.recorder = self.recorder, None
                recorder.close("manual_stopped")
                if recorder.last_error:
                    raise OSError(recorder.last_error)
                response["path"] = str(recorder.path)
            else:
                raise ValueError("알 수 없는 기록 명령")
            response["ok"] = True
        except Exception as exc:
            response = {"id": self.last_request_id, "ok": False, "error": str(exc)}
        try:
            atomic_json(self.directory / ".record-response.json", response)
        except OSError as exc:
            print(f"[RECORD ERROR] 요청 응답 저장 실패: {exc}", flush=True)

    def close(self):
        with self.control_lock:
            self.closed = True
            if self.recorder is not None:
                recorder, self.recorder = self.recorder, None
                recorder.close("monitor_stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("start", "stop"):
        command = commands.add_parser(action)
        command.add_argument("--directory", default=str(Path(__file__).parent / "practices"))
        if action == "start":
            command.add_argument("--label", default="")
    annotate = commands.add_parser("annotate")
    annotate.add_argument("file")
    annotate.add_argument("--success", choices=("yes", "no"))
    annotate.add_argument("--stop-error-cm", type=float)
    annotate.add_argument("--notes")
    args = parser.parse_args()
    if args.action == "annotate":
        path = Path(args.file)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["run_status"] == "running":
            parser.error("기록 중이거나 비정상 종료된 파일입니다. 정상적으로 기록을 닫은 파일만 결과를 추가할 수 있습니다")
        if args.stop_error_cm is not None and not math.isfinite(args.stop_error_cm):
            parser.error("stop-error-cm에는 유한한 숫자를 입력하세요")
        if args.success is not None:
            data["result"]["success"] = args.success == "yes"
        if args.stop_error_cm is not None:
            data["result"]["stop_error_cm"] = args.stop_error_cm
        if args.notes is not None:
            data["result"]["notes"] = args.notes
        data["result"]["annotated_at"] = utc_now()
        atomic_json(path, data)
        print(f"결과 기록: {path}")
        return
    directory = Path(args.directory)
    if not directory.is_dir():
        parser.error("기록 폴더가 없습니다. 웹 모니터 실행과 --directory 경로를 확인하세요")
    request = {"id": uuid.uuid4().hex, "created_at": time.time(), "action": args.action,
               "label": getattr(args, "label", "")}
    atomic_json(directory / ".record-request.json", request)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            response = json.loads((directory / ".record-response.json").read_text(encoding="utf-8"))
            if response.get("id") == request["id"]:
                if not response["ok"]:
                    parser.error(response["error"])
                print(f"기록 {args.action} 확인: {response['path']}")
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    parser.exit(1, "응답을 확인하지 못했습니다. 서버 로그와 권한·경로를 확인하고, 실제 기록 상태를 확인한 뒤 재시도하세요.\n")


if __name__ == "__main__":
    main()
