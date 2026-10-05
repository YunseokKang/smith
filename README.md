# Smith

개인 자산·현금흐름과 거시경제 환경을 함께 분석하는 **조회 전용 개인 자산 자문 에이전트**.
프로젝트 이름은 저장소 이름을 따른다. 별도의 에이전트 브랜드명은 아직 미정이다.

## 현재 상태

구현됨: 설정 검증, 수동 JSON 입력 검증과 SQLite 자산 원장(이력·멱등성·종료·정정·미리보기),
토스증권 조회 전용 연결 점검과 원장 동기화, 순자산·자산 배분·유동성·현금흐름 요약,
ECOS·FRED 금리 근거, 한국은행·Fed 공식 발표 목록, 개인 노출과 근거 연결, 비식별 headless 자문(세 사용 사례),
시각화 HTML 보고서, Gmail 발송, 월·목 정기 발행(작업 스케줄러). 보고서의 서술·제안 계층은 아직 없다.
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
| `toss sync [--dry-run] [--close-missing]` | 토스 보유 주식을 snapshot으로 원장에 저장. 매수 가능 금액·환율은 참고 관측값 |
| `evidence login` / `evidence sync [--days N]` / `evidence show [--as-of DATE]` | ECOS·FRED 금리 근거 저장·조회. 키는 OS 자격 증명 저장소 |
| `advise --case portfolio\|funding\|home [--question] [--show-payload]` | 비식별 payload로 headless 자문. 주택 목표일은 원장의 주택 목표가 있으면 그 날짜, 없으면 실행일로부터 3년 뒤. `--show-payload`는 보낼 내용만 표시 |
| `report preview [--kind monday\|thursday] [--since TIME] [--out PATH] [--narrative] [--research]` | 시각화 보고서 HTML 미리보기(발송 안 함, `reports/`에 저장). `--narrative`는 AI 서술, `--research`는 웹 조사부터 |
| `report run-due` / `report send-now` / `report status` | 정기 회차 발송(스케줄러용, 중복 방지·지연 발송), 즉시 발행, 발행 이력. 발행은 웹 조사와 AI 서술을 포함 |
| `research run\|show\|topics` | 개인 정보 없이 정책·시장 웹 조사 실행·결과 보기·조사 주제 보기 |
| `proposal list` / `proposal accept\|decline\|done KEY [--note]` | 지난 제안 목록, 제안에 대한 결정 기록(다음 보고서의 후속 점검에 반영) |
| `mail login --client-file PATH` / `mail status` / `mail test` / `mail logout` | Gmail 발송 전용 권한 연결·확인·시험 발송. 받는 주소는 Git 제외 `config/smith.local.toml` |
| `summary [--as-of TIME] [--known-at TIME] [--json]` | 순자산, 자산 배분, 유동성, 월 현금흐름, 목표, 데이터 신선도와 경고 |

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
| `src/smith/` | CLI, 설정 검증, 레코드 타입, importer, 원장, 연동 Protocol |
| `tests/` | 설정, 입력 검증, 원장 이력, CLI 테스트 |
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
