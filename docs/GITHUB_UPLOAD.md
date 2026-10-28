# GitHub 업로드 안내

`github_upload` 폴더의 **내용물**을 팀 저장소의 최상위에 올립니다. 상위의 전체 수업 폴더를 업로드하지 않습니다. 이 폴더는 공유용 복사본으로, 원래 작업 폴더와 자동 동기화되지 않습니다. 복사 이후 원본을 수정했다면 변경된 파일을 공유본에도 반영한 뒤 업로드해야 합니다.

이번 준비 작업에서는 Git 저장소 초기화, 커밋, 원격 저장소 설정, 푸시를 수행하지 않았습니다. 저장소 주소와 접근 권한은 팀이 사용하는 것을 적용합니다.

## 가장 간단한 방법

새 GitHub 저장소를 만들거나 팀 저장소를 엽니다. 웹 화면의 파일 업로드 기능으로 다음 항목을 올립니다.

```text
README.md
.gitignore
picar_team/
web_monitor/
docs/
```

업로드 후 첫 화면에 README가 표시되고 `picar_team/config.json`이 바로 보이면 폴더 위치가 맞습니다. `github_upload/github_upload`처럼 중첩시키지 않습니다. `.gitignore`도 빠뜨리지 않습니다.

커밋 메시지 예시:

```text
docs: 팀 코드와 웹 상태 모니터 실행 안내 정리
```

## 새 저장소를 터미널로 올리는 경우

GitHub에서 README나 라이선스 등을 자동 생성하지 않은 **빈 저장소**를 준비한 경우에 해당합니다. PowerShell에서 아래 폴더로 이동합니다.

```powershell
Set-Location -LiteralPath 'C:\Users\rlawo\Desktop\2-2 대학교수업\피지컬컴퓨팅\피지컬컴퓨팅 팀프로젝트\github_upload'
git init -b main
git add README.md .gitignore picar_team web_monitor docs
git status --short
git diff --cached --stat
```

올라갈 파일이 위의 코드와 문서인지 확인한 뒤 진행합니다.

```powershell
git commit -m "docs: 팀 코드와 웹 상태 모니터 실행 안내 정리"
git remote add origin https://github.com/OWNER/REPOSITORY.git
git push -u origin main
```

`OWNER/REPOSITORY`는 실제 팀 저장소 주소로 교체합니다. 커밋 작성자 설정이나 GitHub 로그인이 필요하면 본인 계정으로 설정합니다. 비밀번호·토큰·SSH 개인키를 저장소 파일에 넣지 않습니다.

## 이미 팀 저장소에 코드가 있는 경우

위의 새 저장소 절차를 그대로 실행하지 않습니다. 팀 저장소를 내려받거나 현재 로컬 복사본에서 최신 상태를 확인한 뒤, 이 공유본과 차이를 비교해 필요한 파일만 반영합니다. 기존 README나 수정된 팀원 코드를 통째로 덮어쓰지 않습니다.

반영한 팀 저장소 폴더에서 다음 항목을 검토합니다.

```bash
git status --short
git diff
```

검토한 파일만 `git add`하고 팀이 합의한 브랜치에서 커밋·푸시합니다. 원격 이력과 충돌하면 먼저 차이를 확인해 병합하며, 강제 푸시로 팀원의 변경을 지우지 않습니다.

## 팀원에게 함께 전달할 내용

> 현재 공유본은 자동 미션 코드와 웹 조작 중 상태를 기록하는 보조 모듈입니다. 미션은 시작 QR이 지정한 쪽부터 관찰·집기를 진행합니다. 웹에서 자세를 맞출 때에는 웹 모니터의 로그로 명령 각도를 기록하고, 별도의 하드웨어 시험 때에는 웹 조종 프로그램을 먼저 종료합니다. 아직 각도 50개가 미측정 상태여서 현장에서 보정해야 하며, 전체 자동 실행 전에 README의 알려진 문제를 검토해야 합니다. 먼저 README와 웹 모니터 사용방법을 확인해 주세요.
