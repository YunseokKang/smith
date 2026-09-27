# Smith

개인 자산·현금흐름과 거시경제 환경을 함께 분석하는 **조회 전용 개인 자산 자문 에이전트**.
프로젝트 이름은 저장소 이름을 따른다. 별도의 에이전트 브랜드명은 아직 미정이다.

## 현재 상태

요구사항과 개발 골격만 준비된 단계다. 토스 API 호출, 자산 DB 저장, Claude Code headless 실행,
시황 검색, 보고서 생성, 스케줄 실행, Gmail 발송은 **아직 구현되지 않았다**.
실제 개인정보·잔고·인증정보는 포함하지 않는다. 예시 데이터는 모두 가상이다.

## 빠른 시작

개발 환경은 Windows + PowerShell, Python 3.11 이상이다. 표준 라이브러리 외 의존성은
Windows 시간대 데이터용 `tzdata`뿐이다. 저장소 루트에서 실행한다.

```powershell
python -m pip install --user uv
python -m uv venv --python 3.12 .venv
python -m uv pip install --python .venv -e .

.\.venv\Scripts\smith.exe --help
.\.venv\Scripts\smith.exe check-config --config config/smith.example.toml
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

현재 지원 명령은 `check-config`뿐이다. 설정 확인은 스케줄을 등록하거나 이메일을 보내지 않는다.
환경 문제와 해결 기록은 `docs/dev-notes.md`를 참고한다.

## 구조

| 경로 | 역할 |
|---|---|
| `docs/requirements.md` | 확정 요구사항, 수용 기준, 미결정 항목 |
| `docs/architecture.md` | 제안 아키텍처, 데이터·권한 경계 |
| `docs/data-contract.md` | JSON 입력 및 반복 갱신 계약 초안 |
| `docs/roadmap.md` | 구현 순서와 검증 기준 |
| `docs/dev-notes.md` | Windows 개발 환경, 트러블슈팅 기록 |
| `config/smith.example.toml` | 월·목 06:00 KST 등 기본 설정 |
| `examples/portfolio.example.json` | 가상의 수동 입력 예시 |
| `src/smith/` | CLI, 설정 검증, 연동 Protocol |
| `tests/` | 설정의 안전 기본값·스케줄 검증 |
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
