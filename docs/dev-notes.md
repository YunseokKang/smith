# 개발 노트: 환경과 트러블슈팅

개발 중 겪은 문제와 재발 방지를 위한 정보를 기록한다. 작업 전에 확인하고,
새 문제를 해결하면 같은 변경에서 이 문서에 추가한다.
운영 장애 보고서는 `AGENTS.md`의 규칙에 따라 `docs/bugs/`에 따로 기록한다.

## 개발 환경

- OS: Windows 10. 기본 셸은 PowerShell 5.1이며 Git Bash도 사용할 수 있다.
- Python: 저장소 루트의 `.venv`(Python 3.12, uv로 생성). 시스템 Python은 3.10이라 사용하지 않는다.
- 가상환경은 활성화하지 않고 `.venv\Scripts\python.exe`를 직접 호출한다.
  Windows 기본 실행 정책에서는 `Activate.ps1`이 차단될 수 있기 때문이다.
- 저장소는 `D:`, uv 캐시는 `C:`에 있어 hardlink가 불가하다. `UV_LINK_MODE=copy`로 경고를 없앤다.
- Git for Windows 기본값 `core.autocrlf=true`로 `git add` 시 `LF will be replaced by CRLF` 경고가 나온다.
  저장소에는 LF로 저장되므로 무시해도 된다. `.gitattributes`는 아직 두지 않았다.

### 환경 구성

```powershell
py -3.10 -m pip install --user uv
py -3.10 -m uv venv --python 3.12 .venv
$env:UV_LINK_MODE = "copy"
py -3.10 -m uv pip install --python .venv -e .
```

uv는 시스템 Python 3.10에 설치돼 있으므로 `py -3.10 -m uv`로 부른다(TS-005).

### 검증

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\smith.exe check-config --config config/smith.example.toml
```

editable 설치 후에는 `PYTHONPATH` 설정이 필요 없다.

## 반복 방지 규칙

- 텍스트 파일 입출력은 항상 `encoding="utf-8"`을 명시한다. 사용자가 편집하는 입력 파일은
  `utf-8-sig`로 읽는다. 메모장과 PowerShell 5.1 `Out-File`이 UTF-8 BOM을 붙이기 때문이다.
- SQLite 파일을 쓰는 코드와 테스트는 연결을 반드시 닫는다(`contextlib.closing`).
  Windows는 열린 파일을 삭제할 수 없어 임시 디렉터리 정리가 실패한다.
- 문서의 셸 명령은 PowerShell 문법으로 작성한다. `VAR=x cmd`, `&&`는 PowerShell 5.1에서 동작하지 않는다.
- PowerShell 5.1에서 네이티브 명령에 `2>&1`을 붙이지 않는다. stderr 줄이 `NativeCommandError`로
  감싸져 성공한 실행도 오류처럼 보인다. unittest는 결과를 stderr로 출력하므로 특히 주의한다.
- 시간대는 `zoneinfo`와 `tzdata` 의존성으로 처리한다. 호스트 로컬 시간대에 의존하지 않는다.
- 토스 키는 Windows 자격 증명 관리자(`keyring`, 서비스 `smith.toss`)에만 둔다. 사용자가 직접
  `smith toss login`으로 입력한다. 개발 에이전트는 키를 요청·출력하지 않는다.
- 개발 에이전트가 실행하는 `smith toss check`는 값 숨김 모드로만 실행한다. `--show-values`는
  사용자 본인 터미널에서 앱과 대조할 때만 쓴다.
- 문서나 로그에 사용자 홈 경로 등 개인 식별 경로를 남기지 않는다.

## 트러블슈팅 기록

형식: 증상 → 원인 → 해결 → 예방. 최신 항목을 아래에 추가한다.

### TS-001 `ModuleNotFoundError: No module named 'tomllib'` (2026-09-27)

- 증상: 테스트와 `python -m smith`가 import 단계에서 실패.
- 원인: 시스템 Python이 3.10이다. `tomllib`은 3.11부터 표준 라이브러리에 포함된다.
  `python -m venv`는 기반 인터프리터 버전을 그대로 쓰므로 가상환경만으로는 해결되지 않는다.
- 해결: uv로 Python 3.12를 내려받아 `.venv`를 만든다(위 환경 구성 참고).
- 예방: 항상 `.venv\Scripts\python.exe`로 실행한다.

### TS-002 `ValueError: Invalid timezone` / `No time zone found with key Asia/Seoul` (2026-09-27)

- 증상: Python 3.12 가상환경에서 설정 검증과 테스트 2건이 실패.
- 원인: Windows에는 IANA 시간대 DB가 없고, `zoneinfo`의 대체 원천인 `tzdata` 패키지가 없었다.
- 해결: `pyproject.toml`의 `dependencies`에 `tzdata`를 추가하고 재설치.
- 예방: 시간대 관련 코드는 Windows에서 검증한다. `tzdata` 의존성을 제거하지 않는다.

### TS-003 PowerShell에서 `PYTHONPATH=src python ...` 실행 불가 (2026-09-27)

- 증상: README의 bash식 환경변수 문법이 PowerShell에서 동작하지 않는다.
- 원인: PowerShell은 명령 앞에 붙이는 환경변수 지정을 지원하지 않는다.
- 해결: `pip install -e .`로 editable 설치해 `PYTHONPATH` 자체를 불필요하게 한다.
  필요하면 `$env:PYTHONPATH = "src"`를 먼저 실행한다.
- 예방: 문서의 명령은 PowerShell 기준으로 작성한다.

### TS-004 텍스트 파일 기본 인코딩이 cp949 (2026-09-27, 잠재 문제)

- 증상: 아직 발생하지 않음. 한글이 포함된 TOML·JSON을 인코딩 없이 읽고 쓰면 깨지거나 실패할 수 있다.
- 원인: 한국어 Windows에서 `Path.read_text()`·`open()`의 기본 인코딩은 cp949다.
- 해결: `tests/test_config.py`의 읽기·쓰기에 `encoding="utf-8"`을 명시했다.
  `tomllib.load`는 바이너리 모드로 읽어 영향이 없다.
- 예방: 텍스트 입출력은 항상 인코딩을 명시한다.

### TS-005 `.venv` Python에서 `No module named uv` (2026-10-04)

- 증상: `python -m uv pip install ...`이 `No module named uv`로 실패.
- 원인: VS Code 터미널이 `.venv`를 자동 활성화해 `python`이 `.venv\Scripts\python.exe`를 가리켰다.
  uv는 시스템 Python 3.10에만 설치돼 있다. `$env:VIRTUAL_ENV`로 활성화 여부를 확인할 수 있다.
- 해결: `py -3.10 -m uv ...`로 시스템 Python을 명시한다.
- 예방: 환경 구성 명령은 `python` 대신 `py -3.10`(uv) 또는 `.\.venv\Scripts\python.exe`(프로젝트)로 쓴다.

### TS-006 PowerShell 파이프로 넘긴 JSON에 `Unexpected UTF-8 BOM` (2026-10-04)

- 증상: `smith summary --json | python -c "json.load(sys.stdin)"`가 BOM 오류로 실패.
- 원인: PowerShell 5.1은 네이티브 명령 사이 파이프에서 `$OutputEncoding`으로 다시 인코딩하며 BOM을 붙인다.
  비ASCII 문자도 바뀔 수 있다.
- 해결: 파이프 처리는 Git Bash에서 하거나, 받는 쪽에서 `sys.stdin.buffer.read().decode("utf-8-sig")`로 읽는다.
- 예방: 기계 처리용 출력은 파이프보다 파일이나 Python 내부 호출로 다룬다.

### TS-007 FRED `realtime_start`를 발표일로 오해 (2026-10-04, 리뷰 지적)

- 증상: 과거 관측치까지 발표일이 동기화 날짜로 저장됐다.
- 원인: `realtime_start`는 값이 유효한 실시간 기간의 시작일이며, 기간을 지정하지 않으면 조회 당일이다.
- 해결: `published_on`을 비우고, 로컬 원장의 기존 FRED 행도 `published_on = NULL`로 정리했다.
- 예방: 공급자 필드의 의미는 이름이 아니라 공식 문서 정의로 확인한다
  (https://fred.stlouisfed.org/docs/api/fred/realtime_period.html).

### TS-008 파이프 출력에서 `UnicodeEncodeError: 'cp949'` (2026-10-04)

- 증상: `smith evidence show | ...`가 Fed 제목의 en dash(`–`)에서 중단. Git Bash에서는 한글이 깨져 보임.
- 원인: 한국어 Windows에서 Python은 파이프 출력에 cp949를 쓴다. cp949에 없는 문자는 인코딩할 수 없고,
  Git Bash는 그 바이트를 UTF-8로 읽는다.
- 해결: CLI 시작 시 `sys.stdout.reconfigure(errors="replace")`. Git Bash에서 파이프로 볼 때는
  `PYTHONIOENCODING=utf-8`을 지정한다.
- 예방: 외부 텍스트를 출력하는 명령은 파이프 출력으로도 확인한다.

### TS-009 Python 문자열 치환 스크립트로 코드를 고치다 `\n`이 실제 줄바꿈으로 들어감 (2026-10-04)

- 증상: 편집 후 `SyntaxError: unterminated string literal`.
- 원인: 치환 스크립트의 문자열 안에서 `"\n"` 이스케이프가 해석돼 소스에 실제 줄바꿈이 들어갔다.
- 해결·예방: 이스케이프가 들어 있는 코드는 Edit 도구로 고치고, 스크립트 편집 뒤에는 `ast.parse`로 문법을 확인한다.

### TS-010 `claude`가 `.CMD` 래퍼라 인자가 cmd.exe를 거침 (2026-10-04)

- 증상: `shutil.which("claude")`가 npm의 `claude.CMD`를 반환한다.
- 위험: `.cmd`/`.bat`에 넘긴 인자는 cmd.exe가 다시 해석해 `%`, `&`, `|` 등으로 명령이 주입될 수 있다.
- 해결: 래퍼가 실행하는 `node_modules\@anthropic-ai\claude-code\bin\claude.exe`를 직접 실행하고,
  네이티브 실행 파일을 찾지 못하면 실행을 거부한다(`adviser.find_claude`).

### TS-011 headless 구조화 출력 `error_max_structured_output_retries` (2026-10-04)

- 증상: 실제 payload로 `--json-schema`를 쓰면 종료 코드 1, 결과 subtype이 `error_max_structured_output_retries`.
- 원인: 스키마의 `maxLength`·`maxItems`·`additionalProperties: false`를 모델 출력이 반복해서 어겼다.
- 해결: CLI에는 타입·필수 필드만 넘기고, 받은 뒤 알 수 없는 필드는 버리고(`prune`) 길이·개수를 검증한다
  (`validate`). 제한값과 인용 가능한 참조는 시스템 지침에 명시한다.
- 예방: 실패 시 CLI 결과의 `subtype`을 오류 세부로 남겨 원인을 바로 본다.

### TS-012 보고서 HTML을 눈으로 확인하는 방법 (2026-10-04)

- 상황: 시각화는 검증기(색)만으로 부족하고 실제 렌더링을 봐야 한다. Claude in Chrome 확장은 연결되지 않을 수 있다.
- 방법: Windows 기본 Edge의 헤드리스 스크린샷을 쓴다. 긴 페이지는 개발 전용 Pillow(프로젝트 의존성 아님)로 잘라 본다.
  ```powershell
  Start-Process "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" -Wait -ArgumentList @(
    "--headless=new","--disable-gpu","--hide-scrollbars","--window-size=700,5400",
    "--screenshot=D:\SmithAgent\smith\reports\monday.png","file:///D:/SmithAgent/smith/reports/preview-monday.html")
  ```
- 주의: 스크린샷 경로는 짧은 이름(`MICHAE~1`)이 들어간 임시 폴더보다 `reports\`(Git 제외)가 안정적이었다.

### TS-013 `Set-Content -Encoding UTF8`로 고친 TOML을 읽지 못함 (2026-10-05)

- 증상: `TOMLDecodeError: Invalid statement (at line 1, column 1)`.
- 원인: PowerShell 5.1의 `Set-Content`/`Out-File -Encoding UTF8`은 UTF-8 BOM을 붙인다. 설정 로더가 바이너리로 읽어
  tomllib에 넘겨 BOM을 거부했다(TS-004의 `utf-8-sig` 규칙 미적용).
- 해결: `load_config`가 `utf-8-sig`로 디코딩한다. 설정 파일 편집은 Python이나 Edit 도구를 쓰는 편이 안전하다.

### TS-014 UTC `now.date()`가 현지 날짜와 다름 (2026-10-05, 리뷰)

- 증상: 월요일 06:00 KST 정기 보고에서 10월 5일 시작 월급이 0원으로 계산됐다. 같은 시각을 KST로 넘기면 정상.
- 원인: 월요일 06:00 KST는 UTC 일요일 21:00이다. `datetime.now(timezone.utc)`를 `as_of`로 넘기면 요약·타임라인의
  `as_of.date()`가 하루 전 날짜가 된다.
- 해결: 계산 진입점(`build_report`, `advise`, `summary`)에 넘기는 `as_of`는 `config.local_time()`으로 현지 시각으로
  바꾼다. 저장은 `_db_time`이 UTC로 정규화한다. 날짜 경계 테스트는 KST 이른 아침 시각을 UTC로 바꿔 넣어 확인한다.

### TS-015 `pythonw` 실행에는 콘솔이 없음 (2026-10-05, 리뷰)

- 증상: 작업 스케줄러의 `pythonw -m smith report run-due`가 설정·자격 증명 오류로 끝나도 아무 흔적이 없다.
- 원인: `pythonw`의 `sys.stdout`은 `None`이고 `print`는 조용히 버려진다.
- 해결: 무인 실행 결과는 `data/report-runs.log`에 덧붙이고, 회차가 정해진 뒤의 실패는 원장에 남긴다.
  `report status`가 로그 위치를 알려 준다. 작업은 배터리 전환 시 중단하지 않도록 설정했다
  (`StopIfGoingOnBatteries=False`).

### TS-016 헤드리스 Edge로는 500px보다 좁은 화면을 볼 수 없음 (2026-10-05)

- 증상: `--window-size=390,...` 스크린샷이 잘려 보였지만, 실제 레이아웃 폭은 504px이었다.
- 원인: headless 창의 최소 폭이 약 500px이다. 이미지만 390px로 잘린다.
- 해결: 보고서를 `<iframe style="width:375px">`로 감싼 페이지를 찍는다. 넘침을 찾을 때는 `getBoundingClientRect().right`가
  화면 폭을 넘는 요소를 페이지 위에 출력하는 스크립트를 넣고 스크린샷으로 읽는다(`--dump-dom` 출력은 PowerShell에서 비어 있었다).

### TS-017 Git Bash heredoc이 "unexpected EOF while looking for matching quote"로 실패 (2026-10-05)

- 증상: `python - <<'PYEOF' ... PYEOF`처럼 따옴표 친 heredoc 안에 Python 편집 스크립트를 넣었는데, 내용에 따라
  Bash가 따옴표 짝을 찾다가 실패했다(같은 형식이 다른 내용에서는 성공해 원인을 특정하지 못함).
- 해결: 길거나 따옴표가 많은 편집 스크립트는 Write 도구로 scratchpad에 `.py` 파일을 만든 뒤 실행한다.
  Python 문자열에 `\x7f` 같은 이스케이프를 쓰면 실제 제어문자로 저장될 수 있으니, 저장 후 `repr`로 확인한다.

### TS-018 Windows에서 `write_text`로 되쓴 설정 파일이 TOML 오류 (2026-10-05)

- 증상: `config/smith.local.toml`에 구역을 덧붙인 뒤 `TOMLDecodeError: Found invalid character '\r'`.
- 원인: 파일이 CRLF였는데 `read_bytes().decode()`로 읽은 문자열을 `Path.write_text`로 쓰자, Windows가 `\n`을 다시
  `\r\n`으로 바꿔 줄 끝이 `\r\r\n`이 됐다. 정기 작업이 15분마다 이 파일을 읽으므로 즉시 운영 장애가 된다.
- 해결: 줄 끝을 정규화한 뒤 `write_bytes`로 쓴다(또는 `open(..., newline="")`). 설정을 고친 뒤에는 반드시
  `load_config`로 다시 읽어 확인한다.

### TS-019 첫 메일 답변이 검증 과잉으로 대체 문장만 나감 (2026-10-05)

- 증상: "포트폴리오 어떻게 평가해?" 질문에 내용 좋은 답(1,500자)이 두 번 다 탈락해 "다음 보고서에서" 문장만 발송.
- 원인: ① 세금·규제 단어가 나오면 공식 출처를 요구했는데 Smith 자체 세금 계산을 인용할 방법이 없었다.
  ② "1억 350만 원"을 숫자 `1`과 `350만`으로 쪼개 원장 금액 103,500,000과 대조하지 못했다. ③ 답변 분량 기준이 없었다.
- 해결: 금액 표기를 원 단위로 환산해 정밀도 범위로 대조, 참조 `tax` 허용, 질의응답 분량 지침과 1,500자 상한,
  통과한 문장만 살리는 문장 단위 구제. 검증 규칙을 바꿀 때는 `advice_runs`에 남은 원시 출력으로 재검증해 본다.
- 후속(2026-10-05): 사용자 판단에 따라 탈락 대신 "자동 점검 메모"를 붙여 보내는 방식으로 바꿨다(보고서·답변 모두
  고객 한 사람용). 문장 단위 구제는 없앴다. 검증 규칙은 그대로 돌고, 결과가 삭제에서 표시로 바뀐 것이다.

### TS-020 실거래가 동일 거래가 한 행으로 합쳐져 건수가 줄고, 취소가 반영되지 않음 (2026-10-05)

- 증상: 13개월 304건을 받았는데 290행만 저장. 이미 저장한 거래가 나중에 해제·삭제돼도 원장에 남는다.
- 원인: 동·호수를 저장하지 않으므로 같은 날·층·금액·면적의 서로 다른 계약이 같은 키가 됐다. 저장은 추가만 했다.
- 해결: 키별로 `active_count`·`cancelled_count`를 세고(원장 v13), 받아 온 (주택, 종류, 월)은 통째로 지우고 다시
  넣는다. 분석은 건수만큼 펼쳐 중간값을 낸다. 설정에서 빠진 주택의 거래도 지운다.

### TS-021 PowerShell 5.1 `Get-Content`/`Set-Content`로 고친 파일의 한글이 깨짐 (2026-10-05)

- 증상: `(Get-Content f -Raw).Replace(...) | Set-Content f`로 테스트 파일 한 줄을 바꾸자 `SyntaxError: unterminated string`.
- 원인: Windows PowerShell 5.1은 BOM 없는 UTF-8 파일을 시스템 코드 페이지(cp949)로 읽어, 한글이 깨진 채 다시 쓴다.
- 해결: `git checkout`으로 복구. 파일 편집은 편집 도구나 `[IO.File]::ReadAllText(path, [Text.Encoding]::UTF8)` +
  `WriteAllText(path, text, (New-Object Text.UTF8Encoding $false))`, 또는 Python 스크립트(`read_bytes().decode`)로 한다.

### TS-022 `schtasks /Query` 출력이 실행 방식에 따라 언어·인코딩이 다름 (2026-10-05)

- 증상: `smith doctor`의 스케줄러 상태가 모두 `?`. 콘솔에서는 영어 필드명(UTF-8)인데, 콘솔 없이
  (`CREATE_NO_WINDOW`) 실행하면 한국어 필드명(cp949)으로 나온다. `text=True`는 cp949 디코딩 오류로 실패했다.
- 해결: PowerShell `Get-ScheduledTask`·`Get-ScheduledTaskInfo` 결과를 `ConvertTo-Json`으로 받아 고정된 필드명으로 읽는다.

### TS-023 스크립트 문자열 치환이 일부 파일에서만 "찾을 수 없음"으로 실패 (2026-10-07)

- 증상: Python으로 `read_bytes().decode()` 후 여러 줄 문자열을 `replace`하는 편집이 어떤 파일에서는 되고 어떤 파일에서는
  일치 0건으로 실패했다(`payload.py`, `docs/architecture.md` 등).
- 원인: 작업 트리의 줄바꿈이 파일마다 다르다(`core.autocrlf = true`라 체크아웃 파일은 CRLF, 도구가 새로 쓴 파일은 LF).
  `\n`으로 쓴 검색 문자열은 CRLF 파일과 맞지 않는다. Git Bash의 `grep -c $'\r$'`·`sed -n p | od`는 CR을 제대로
  보여 주지 않아 판별에도 쓸 수 없었다.
- 해결: 여러 줄 편집은 편집 도구로 한다(줄바꿈을 보존). 판별은 PowerShell
  `[regex]::Matches([IO.File]::ReadAllText(path), "`r`n").Count`로 한다. `sed -i`로 한 줄을 바꿀 때 `\r?$`를 쓰면 그 줄의
  CR이 사라져 줄바꿈이 섞이니 피한다. 커밋 시에는 `autocrlf`가 LF로 맞추므로 저장소 내용은 영향이 없다.
