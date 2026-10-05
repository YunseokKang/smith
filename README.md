# Smith

개인 자산·현금흐름과 거시경제 환경을 함께 분석하는 **조회 전용 개인 자산 자문 에이전트**.
프로젝트 이름은 저장소 이름을 따른다. 별도의 에이전트 브랜드명은 아직 미정이다.

## 현재 상태

구현됨: 설정 검증, 수동 JSON 입력 검증과 SQLite 자산 원장(이력·멱등성·종료·정정·미리보기),
토스증권 조회 전용 연결 점검과 원장 동기화, 순자산·자산 배분·유동성·현금흐름 요약,
ECOS·FRED 금리 근거, 한국은행·Fed 공식 발표 목록, 개인 노출과 근거 연결, 비식별 headless 자문(세 사용 사례),
시각화 HTML 보고서, Gmail 발송, 월·목 정기 발행(작업 스케줄러), 결정적 제안 계층과 제안 이력, 웹 조사 브리프(출처 페이지
대조), 코드 검증을 거친 AI 서술, 세금 관점 전략, 국토부 실거래가, 메일 답장 질의응답, 보고서 계보(lineage), `doctor` 점검.
진행 상황은 `docs/roadmap.md`.
실제 개인정보·잔고·인증정보는 포함하지 않는다. 예시 데이터는 모두 가상이다.

## 빠른 시작

개발 환경은 Windows + PowerShell, Python 3.11 이상이다. 표준 라이브러리 외 의존성은
Windows 시간대 데이터용 `tzdata`와 OS 자격 증명 저장소용 `keyring`이다. 저장소 루트에서 실행한다.

```powershell
py -3.10 -m pip install --user uv
py -3.10 -m uv venv --python 3.12 .venv
py -3.10 -m uv pip install --python .venv -e .

.\.venv\Scripts\smith.exe --help
.\.venv\Scripts\smith.exe check-config --config config/smith.example.toml
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

| 명령 | 동작 |
|---|---|
| `check-config --config FILE` | 설정 검증. 스케줄을 등록하거나 이메일을 보내지 않는다 |
| `import FILE [--dry-run] [--db PATH]` | JSON 검증 후 원장에 원자적으로 적용. `--dry-run`은 계획만 표시 |
| `records [--as-of TIME] [--known-at TIME] [--db PATH]` | 지정 시각에 유효한 레코드 목록. `--known-at`은 그 시각까지 기록된 내용만 사용 |
| `toss login` / `toss logout` | 토스 client ID·secret을 OS 자격 증명 저장소에 저장·삭제. 입력은 화면에 표시되지 않는다 |
| `toss check [--show-values]` | 허용된 조회 API를 한 번씩 호출해 연결 점검. 기본은 금액·종목을 숨긴다 |
| `toss sync [--dry-run] [--close-missing] [--allow-empty]` | 토스 보유 주식을 snapshot으로 원장에 저장. 매수 가능 금액·환율은 참고 관측값. `--close-missing`은 매도된 종목을 종료(정기 실행은 항상 사용), 보유 종목이 0개인 응답은 `--allow-empty`가 있을 때만 반영 |
| `evidence login` / `evidence sync [--days N]` / `evidence show [--as-of DATE]` | ECOS·FRED 금리 근거 저장·조회. 키는 OS 자격 증명 저장소 |
| `advise --case portfolio\|funding\|home [--question] [--show-payload]` | 비식별 payload로 headless 자문. 주택 목표일은 원장의 주택 목표가 있으면 그 날짜, 없으면 실행일로부터 3년 뒤. `--show-payload`는 보낼 내용만 표시 |
| `report preview [--kind monday\|thursday] [--since TIME] [--out PATH] [--narrative] [--research]` | 시각화 보고서 HTML 미리보기(발송 안 함, `reports/`에 저장). `--narrative`는 AI 서술, `--research`는 웹 조사부터 |
| `report run-due` / `report send-now` / `report status` / `report activate` | 정기 회차 발송(스케줄러용, 중복 방지·지연 발송), 즉시 발행, 발행 이력, 정기 발행 시작(이미 지난 회차는 보내지 않음). 발행은 웹 조사와 AI 서술을 포함. 설정의 요일·시각을 바꾸면 다음 정기 실행부터 새 일정이 적용되고 지난 회차는 소급하지 않는다 |
| `backup [--out DIR] [--keep N]` | 원장의 일관된 사본을 만들고 무결성 검사를 통과한 것만 남긴다(기본 `data/backups`, 14개). `[backup] dir`을 설정하면 정기 발송 뒤 자동으로 만든다. 사본은 실제 재무 정보이므로 외장 디스크 등 직접 관리하는 곳에 둔다 |
| `research run\|show\|topics` | 개인 정보 없이 정책·시장 웹 조사 실행·결과 보기·조사 주제 보기 |
| `proposal list` / `proposal accept\|decline\|done KEY [--note]` | 지난 제안 목록, 제안에 대한 결정 기록(다음 보고서의 후속 점검에 반영) |
| `realestate login\|status\|logout\|sync\|show` | 공공데이터포털·R-ONE 키 저장(화면 비표시), 보유 단지 실거래가 수집과 시세·전세 요약 |
| `mail login --client-file PATH [--read]` / `mail status` / `mail test` / `mail answer` / `mail logout` | Gmail 권한 연결·확인·시험 발송. `--read`와 `[mail] answer_replies = true`면 보고서 메일에 대한 답장 질문에 답한다. 받는 주소는 Git 제외 `config/smith.local.toml` |
| `summary [--as-of TIME] [--known-at TIME] [--json]` | 순자산, 자산 배분, 유동성, 월 현금흐름, 목표, 데이터 신선도와 경고 |
| `doctor` | 원장·설정·자격 증명(존재 여부만)·작업 스케줄러·최근 동기화·조사·발송·메일 답변·실행 로그를 한 번에 점검. 아무것도 보내거나 쓰지 않는다 |

정기 실행(Windows): `powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1`가 `Smith\ReportDue`
작업(15분마다 `report run-due`)을 등록·갱신한다(`-Remove`로 삭제). 등록 뒤 `smith doctor`로 확인한다.

기본 원장은 `data/smith.db`(Git 제외). 입력 형식은 `docs/data-contract.md`,
환경 문제와 해결 기록은 `docs/dev-notes.md`를 참고한다.

## 구조

| 경로 | 역할 |
|---|---|
| `docs/requirements.md` | 확정 요구사항, 수용 기준, 미결정 항목 |
| `docs/architecture.md` | 제안 아키텍처, 데이터·권한 경계 |
| `docs/data-contract.md` | JSON 입력 계약 v1과 revision·정정 규칙 |
| `docs/toss-openapi.md` | 토스증권 Open API 확인 결과와 설계 반영 사항 |
| `docs/roadmap.md` | 구현 순서와 검증 기준 |
| `docs/dev-notes.md` | Windows 개발 환경, 트러블슈팅 기록 |
| `config/smith.example.toml` | 월·목 06:00 KST 등 기본 설정 |
| `examples/portfolio.example.json` | 가상의 수동 입력 예시(자산·부채·현금흐름·목표) |
| `src/smith/` | CLI, 설정 검증, 원장, 동기화, 계산·제안·세금, 조사·서술·검증, 보고서·발송, 질의응답 |
| `scripts/install-schedule.ps1` | Windows 작업 스케줄러 등록(재현 가능한 설치) |
| `tests/` | 위험 중심 단위 테스트(네트워크·실제 메일·모델 호출 없음) |
| `.github/workflows/tests.yml` | push·PR마다 Windows에서 전체 테스트와 예시 설정 검증 |
| `AGENTS.md`, `CLAUDE.md` | 개발 에이전트 작업 지침 |

## 운영 원칙

- 금융계좌는 조회 전용. 금융기관 쓰기 API와 Hermes 매매 명령을 제공하지 않는다.
- 금액 계산은 코드가 수행하고, LLM은 검증된 수치와 근거로 해석한다.
- 개인 식별정보·API 키·이메일 인증정보를 LLM이나 로그에 전달하지 않는다.
- 실제 데이터는 Git 밖에 보관한다. `.env`, `data/`, `reports/`, `secrets/`는 Git에서 제외한다.
- 데이터가 없거나 오래되면 명시한다. 조회 실패를 0원으로 간주하지 않는다.

## 저장소 반영 상태

대상: https://github.com/YunseokKang/smith
원격 저장소를 확인한 결과 초기 README 한 줄 외에 기존 파일·지침이 없어, 충돌 없이 골격을 반영했다.
