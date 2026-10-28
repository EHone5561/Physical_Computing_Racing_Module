# 시연별 JSON 기록

목적은 매 시연의 설정과 시간별 상태를 보존해, 나중에 성공 여부·정지 오차·소요 시간과 비교하고 적절한 제어값을 찾는 것입니다. `config.json`은 실행 설정이며, `practiceN.json`은 해당 설정으로 실행한 이력입니다. 기록기가 설정이나 최적값을 자동 변경하지 않습니다.

## 저장되는 내용

| 항목 | 의미 |
| --- | --- |
| `mode`, `label` | 미션·직접 시험·웹 조작 구분과 웹 시연 이름 |
| `started_at`, `ended_at` | UTC 기준 시작·종료 시각. 한국 시간은 UTC+9 |
| `run_status` | `running`, `completed`, `interrupted`, `error`, `manual_stopped`, `monitor_stopped` |
| `config_snapshot` | 그 시연 시작 당시 설정 복사본 |
| `result` | 사람이 나중에 기록하는 성공 여부, 정지 오차, 설명 |
| `samples` | 시간순 상태 목록 |

각 `samples` 항목은 다음과 같습니다.

| 필드 | 의미와 단위 |
| --- | --- |
| `seq` | 시연 안에서 증가하는 표본 번호 |
| `elapsed_s` | 기록 시작 이후 경과 시간, 초. 출발 QR 인식 때 초기화하지 않음 |
| `timestamp` | 표본 기록 시각, UTC |
| `kind` | `periodic`: 주기 표본, `command`: 명령 변경, `event`: 단계·QR·시작·종료 |
| `event`, `details` | 명령 또는 상황 이름과 추가 정보 |
| `phase` | 주행·미세 보정·좌우 관찰·집기·놓기 등의 현재 단계 |
| `servo_deg` | `steer`, `base`, `arm`, `wrist`, `grip`의 마지막 성공한 명령 각도, 도 |
| `motor_throttle` | 모터별 마지막 명령 출력. 팀 코드 M1·M2, 웹 모니터 M1~M4 |
| `speed_command` | 팀 코드에서 마지막으로 적용 완료한 주행 출력 -100~100. 웹에서는 `null` |
| `speed_setting` | 웹의 속도 설정. 팀 코드에서는 `null` |
| `wheels_locked` | 팀 코드의 바퀴 잠금 여부. 웹에서는 `null` |
| `distance_cm` | 초음파 라이브러리에서 마지막으로 읽은 거리, cm |
| `distance_read_age_s` | 그 거리 값을 프로그램이 읽은 뒤 지난 시간, 초 |
| `distance_error` | 거리 읽기 오류. 성공 시 `null` |

서보 명령 각도는 실제 관절 각도나 실제 바퀴 조향각을 측정한 값이 아닙니다. 모터 출력도 실제 주행 속도(cm/s)나 RPM이 아닙니다. 실제 각도·속도를 분석 목표로 삼으려면 별도 측정값이 필요합니다. `null`은 아직 관찰하지 못했거나 해당 방식에서 제공하지 않는 값입니다.

거리 읽기에 실패하면 마지막 성공 값과 오류를 함께 남깁니다. `distance_read_age_s`는 초음파 내부의 새 샘플 시각이 아닙니다. 최근 읽기에 성공해도 라이브러리 내부의 이전 값일 수 있습니다. 여러 관절·모터·거리의 상태는 동시에 측정한 값이 아니라 캐시를 모은 상태입니다.

## 팀 코드에서 자동 저장

기존처럼 Pi의 `picar_team` 폴더에서 실행합니다.

```bash
python3 hw.py servo
python3 hw.py dist
python3 hw.py qr
python3 hw.py motor 15 0.2
python3 mission.py
```

각 명령은 **별도로** 실행합니다. 실제 제어를 시작하기 전에 기존 웹 조종 프로그램을 종료해야 합니다. 프로그램이 초기화를 시작할 때 다음 파일을 만들고, 정상 종료·`Ctrl+C`·처리되는 예외 시 마지막 상태와 종료 사유를 저장합니다.

```text
picar_team/
├── config.json
├── hw.py
├── mission.py
├── practice_logger.py
└── practices/
    ├── practice1.json
    ├── practice2.json
    └── practice3.json
```

`mission.py`가 설정의 `null` 때문에 실행을 거부하거나, 하드웨어 라이브러리를 불러오기 전에 실패하면 시연 기록은 시작하지 않습니다. 초기화가 시작된 뒤 I2C 등의 오류가 발생하면 오류 종료 기록을 남깁니다. 기록 파일을 처음 만들 권한이나 공간이 없으면 초기화 전에 오류가 발생합니다.

`config.json`에 추가된 부분:

```json
"recording": {
  "enabled": true,
  "directory": "practices",
  "interval_s": 0.1,
  "flush_s": 1.0
}
```

- `enabled`: 팀 코드 자동 기록 여부.
- `directory`: 상대 경로이면 `config.json`이 있는 폴더 기준. 절대 경로도 사용 가능.
- `interval_s`: 주기 표본 간격. 기본 0.1초로 약 초당 10개이며, 정확한 실시간 주기를 보장하지 않음.
- `flush_s`: 파일 갱신 목표 간격. 기본 1초. 종료 시에도 최종 저장.

주기 표본 외에 서보·모터 명령이 바뀐 순간도 기록합니다. 따라서 0.08초 전진 같은 짧은 명령도 별도 항목으로 남습니다. 팔이 움직이거나 서보 도구에서 입력을 기다리는 동안에도 기존 초음파 객체를 별도 읽기 스레드에서 읽습니다.

`verbose: false`는 터미널 상태 출력을 줄이는 설정입니다. JSON 기록은 별도의 `recording.enabled`로 제어합니다.

## 웹 수동 조작을 시연별로 저장

기존 웹 모니터를 사용하는 경우에도 **파일 3개를 함께 갱신**합니다.

```text
실제 제조사 Server 폴더/
├── WebServer.py
├── web_status.py
├── practice_logger.py
└── recording.json
```

이 세 파일은 공유 폴더의 `web_monitor`에서 가져옵니다. 실제 `WebServer.py`에 넣는 두 줄은 기존과 같습니다.

```python
import web_status
web_status.start(RPIservo, move, functions.ultra, lambda: speed_set)
```

서비스를 한 번 재시작해 갱신된 모니터를 적용합니다. 이후 시연마다 서버를 재시작할 필요가 없습니다. 웹 조종 서비스는 계속 켜 둡니다. 모니터가 켜진 상태에서 SSH 터미널을 **실제 Server 폴더로 이동**한 뒤 다음 순서로 사용합니다.

```bash
# 1. 기록 시작. 서버의 확인 응답과 파일 경로가 나오면 웹 조작 시작
sudo python3 practice_logger.py start --label left-first-test

# 2. 웹에서 조작한다. 한 시연을 마치면 웹에서 로봇을 멈춘다.

# 3. 기록 종료 및 JSON 저장
sudo python3 practice_logger.py stop

# 4. 다음 시연 기록
sudo python3 practice_logger.py start --label right-first-test
```

`start`/`stop`은 **기록만** 제어하며 로봇을 움직이거나 멈추지 않습니다. 기록 중 다시 `start`하면 파일을 갈라버리지 않고 오류를 반환하므로 먼저 `stop`합니다. 여러 SSH 창에서 동시에 기록 명령을 보내지 말고, 한 명이 응답을 확인하며 순서대로 실행합니다. 기본 웹 동작은 터미널 상태 출력만 시작하며, JSON 파일은 `start` 이후 생성됩니다.

제공된 제조사 서비스가 root로 실행되므로 위 예시는 `sudo`를 사용합니다. 다른 사용자로 서비스가 실행된다면 같은 파일 접근 권한을 가진 계정으로 실행합니다. CLI가 응답을 확인하지 못했다면 서버 로그와 실제 기록 상태를 먼저 확인합니다. 응답 실패가 기록 시작 실패를 뜻하지는 않습니다.

웹 기록은 `Server/practices`에 저장됩니다. 웹의 주기와 경로는 `recording.json`에서 설정합니다. 경로를 바꿨으면 명령에도 같은 경로를 지정합니다.

```bash
sudo python3 practice_logger.py start --directory /home/실제계정/practices --label trial
sudo python3 practice_logger.py stop --directory /home/실제계정/practices
```

번호는 **저장 폴더별로 기존 최대 번호 + 1**입니다. 팀 코드와 웹 기록을 같은 번호 순서로 관리하려면 두 설정의 `directory`를 같은 절대 경로로 맞추고 해당 폴더의 쓰기 권한도 맞춥니다. 기존 파일을 지우면 빈 번호를 영구 보존하는 카운터는 아니므로 기록은 따로 보관합니다.

웹 기록은 제공된 원본의 `ServoCtrl.set_angle()`, `Move.Motor()`, `motorStop()`, `setup()` 호출을 관찰합니다. 다른 경로에서 하드웨어를 직접 제어하는 수정본은 추가 연결이 필요합니다. 아직 관찰하지 못한 명령은 `null`입니다. 웹 모드의 `config_snapshot`에는 모니터 설정과 출처 설명이 들어가며, 사용하지 않는 팀 `config.json`을 실제 설정처럼 넣지 않습니다.

## 결과를 추가해 분석 준비

시연 기록을 종료한 뒤 다음처럼 결과를 추가할 수 있습니다. 아래 성공 여부와 오차는 **사용법 예시**이며 실제 관찰한 값으로 바꿉니다.

```bash
python3 practice_logger.py annotate practices/practice1.json --success yes --stop-error-cm -1.2 --notes "정지 위치 확인, 물체 이동 성공"
```

웹 서버가 root 소유로 만든 파일이라면 이 명령도 `sudo`로 실행합니다. 명령은 시간별 표본을 유지하고 `result`만 갱신합니다. 기록 중인 파일은 수정하지 않습니다.

- `success`: 실제 관찰한 미션 성공 여부. `yes` 또는 `no`.
- `stop_error_cm`: 실제 정지 거리 − 목표 거리. 목표보다 멀면 양수, 가까우면 음수. 같은 기준으로 잰 값을 사용.
- `notes`: 배터리 상태, 물체 위치, 카메라 가림, 집기 실패 등 시연 설명.

`run_status: completed`는 프로그램이 정상적으로 끝났다는 뜻입니다. 물체를 실제로 제대로 옮겼는지는 알 수 없으므로 `result.success`는 자동으로 `true`가 되지 않습니다.

후속 분석에서는 다음을 구분합니다.

1. 시간별 분석은 `samples`를 사용하고, 시간 간격은 실제 `elapsed_s` 차이로 계산합니다.
2. 명령이 많은 순간에는 추가 표본도 많습니다. 일정한 간격의 데이터가 필요하면 `kind == "periodic"`을 고르거나 동일한 시간 간격으로 재표본화합니다.
3. 모터 출력·초기 거리 등의 입력과 실제 정지 오차·소요 시간 등의 결과를 연결합니다. 값만 모으고 결과를 기록하지 않으면 어떤 설정이 나았는지 비교하기 어렵습니다.
4. 주행과 팔 작업을 `phase`로 나눕니다. 센서가 다른 방향을 보는 동안의 거리 변화를 전진 이동으로 해석하지 않습니다.
5. 같은 시연의 인접 표본은 서로 비슷하므로, 모델 평가용 데이터는 시연 단위로 나눠 새 시연에서도 맞는지 확인합니다. 선형회귀가 맞는지와 최적값은 실제 데이터를 모은 뒤 판단합니다.

## 저장 방식과 제한

표본은 메모리에 모으고 별도 스레드가 일정 간격으로 완성된 JSON을 임시 파일에 쓴 뒤 교체합니다. 서보·모터 명령마다 파일 전체를 쓰지는 않습니다. 갑작스러운 전원 차단이나 강제 종료에서는 마지막 저장 이후의 표본과 종료 정보가 유실될 수 있으며, 남은 파일의 `run_status`가 `running`일 수 있습니다. 디스크 용량 부족 등 저장 실패는 `[RECORD ERROR]`로 표시합니다.

현재 방식은 짧은 경기 시연을 위한 것입니다. 파일 전체를 주기적으로 갱신하고 표본을 메모리에 보관하므로 몇 시간씩 계속 기록하면 부담이 커질 수 있습니다. 웹에서는 한 시연이 끝날 때마다 `stop`하고 다음 시연을 새 파일로 나눕니다. 저장 부하에 따른 실제 주기와 주행 영향은 Pi에서 확인해야 합니다.

저장된 기록은 기본 `.gitignore`에서 제외합니다. 분석 자료를 팀과 공유할 때에는 필요한 파일을 선별해 별도 데이터 폴더로 복사하거나 명시적으로 Git에 추가합니다. 테스트에서 생성한 가짜 기록은 실제 실험 자료에 포함하지 않습니다.

## 이번 변경과 검증

| 파일 | 변경 |
| --- | --- |
| `practice_logger.py` | 번호 자동 부여, 시간순 JSON 저장, 웹 기록 제어 CLI, 결과 추가 CLI |
| `config.json` | `recording` 설정 추가. 기존 각도·속도·거리 값 유지 |
| `hw.py` | 자동 기록 시작·종료, 명령 순간 기록, 거리 캐시 갱신, 초기화 오류 종료 처리 |
| `mission.py` | 단계와 QR 판단 기록, 정상·중단·오류 종료 구분. 미션 순서 유지 |
| `web_status.py` | 기존 웹 명령 관찰, SSH로 시연별 기록 시작·종료 |
| `web_monitor/recording.json` | 웹 기록 폴더와 주기 설정 |
| `tests/test_recording.py` | 하드웨어 없이 기록 동작을 검증하는 테스트 |

`practice_logger.py`는 두 배포 폴더에 같은 내용으로 둡니다. 웹 서버에는 해당 폴더만 따로 복사할 수 있도록 한 구성입니다. 이 파일을 수정할 때에는 두 복사본을 함께 갱신합니다.

저장소 최상위에서 테스트:

```bash
python3 -m unittest discover -s picar_team/tests -v
```

15개 모의 테스트에서 번호 충돌 방지, 짧은 명령, 주기 저장, 오류 시 기존 JSON 보존, 결과 추가, 웹 기록 제어, 초기화 실패 처리와 대기 중 거리 기록을 확인했습니다. 실제 Raspberry Pi, GPIO/I2C, 웹서버와의 연결 시험은 별도로 필요합니다. 기존의 QR 실패 시 기본 위치 진행과 정지 보정 실패 판정 문제는 이번 기록 기능에서 수정하지 않았습니다.
