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
