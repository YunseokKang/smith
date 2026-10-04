# 아키텍처 초안

Python 3.11+를 골격의 구현 언어로 선택했다. 아래 DB·검색·어댑터 구성은 설계 제안이며
외부 연동 구현이나 검증을 완료했다는 뜻이 아니다.

## 계층

1. 입력: 토스 조회 어댑터 / JSON importer / 이후 Hermes 조회 어댑터.
2. 정규화·저장: 원천 식별, 중복 제거, 거래/시점별 평가/버전 이력.
3. 계산: 순자산, 현금흐름, 집중도, 조달 비용, 목표 시나리오.
4. 근거: 금리·시황 공급자와 문서 검색. 발표 시점·관측 시점·출처를 보존.
5. 자문: 비식별 allowlist payload + 계산 결과 + 검색 근거 → Claude Code headless.
6. 출력: CLI 응답, 보고서 미리보기, 명시적 발행, 스케줄러, Gmail delivery.

CLI와 향후 앱은 동일한 application service를 호출한다. 웹 서버를 지금 만들 필요는 없다.
`src/smith/ports.py`는 초기 의존성 경계를 표현하는 Protocol이다.

## 저장소 및 RAG

PoC 원장은 표준 라이브러리 `sqlite3`로 구현했다(`src/smith/ledger.py`, 기본 `data/smith.db`).
선택 이유: 의존성 없음, 단일 사용자 규모, 파일 하나로 백업 가능, transaction 지원.
배포 환경과 동시 실행 요구가 확정되면 재검토한다.
원장은 record revision을 추가만 하는 구조다. 공통 열(ID, kind, owner, source, status, 시각,
revision)은 컬럼으로, 종류별 필드는 정규화된 JSON 텍스트로 저장해 계약 확장 시 migration을 줄인다.
가져오기는 `BEGIN IMMEDIATE` transaction으로 직렬화하며, 스키마 버전은 `PRAGMA user_version`으로 관리한다.
스키마 생성은 쓰기 경로(`import`)에서만 한다. 미리보기와 조회는 원장을 읽기 전용(`mode=ro`)으로 열고,
초기화되지 않은 파일이면 빈 메모리 원장으로 계획한다.
잔고·현금흐름·거래는 구조화된 DB에 저장한다. RAG는 약관·상품 문서·외부 자료·과거 자문을
검색하는 데 사용하며 수치의 원장으로 사용하지 않는다. 문서가 적으면 전문 검색부터 시작해
필요할 때 임베딩 검색을 추가한다. 출처, 문서 버전, 유효 기간과 인용 위치를 보존한다.

## 권한 경계

금융 API 키는 어댑터만 접근한다. 조회 전용 권한을 공급자가 지원하면 우선 사용하고,
미지원이면 명시적으로 허용한 조회 endpoint만 어댑터에서 호출한다.
인증 token 발급의 POST와 주문 POST를 혼동하지 않는다. 주문 기능 자체는 제공하지 않는다.
토스증권 Open API는 scope가 없어 같은 token으로 주문 API도 호출된다. 따라서 토스 어댑터는
`docs/toss-openapi.md`의 허용 목록 밖 요청을 네트워크 호출 전에 거부하고, 이를 테스트로 검증한다.

Claude Code headless 실행은 비밀정보나 운영 DB에 직접 접근할 수 없는 별도 작업 경계에서
수행한다. 범용 shell과 임의 HTTP를 자문 도구로 노출하지 않는다. 실제 CLI 버전의 권한·도구
제한 방식을 공식 문서로 확인한다. `--dangerously-skip-permissions`를 기본값으로 삼지 않는다.
Claude Code에서 Smith 기능을 호출하는 경로와 Smith가 headless 추론을 호출하는 경로를
분리해 자기 자신을 재귀 호출하지 않도록 한다.

LLM이 작성한 텍스트는 발송 대상·스케줄·금융 API 호출을 변경할 수 없다.
메일 수신자는 신뢰된 로컬 설정에서만 읽는다. 로그에는 잔고 원문과 인증정보를 남기지 않는다.

## 스케줄 및 복구

시간대 인식 스케줄러가 설정된 현지 요일·시각을 해석한다. 호스트 로컬 시간대에 의존하지 않는다.
지정 발행 슬롯/요청 ID, 데이터 snapshot ID, 분석 버전, 발송 상태를 저장한다.
수동 보고는 정기 보고의 슬롯을 소모하지 않는다. 재시작 후 missed run 정책은 구현 전에 결정한다.
`enabled=false`가 예시 기본값이다. 실제 설정과 발송 테스트를 완료한 뒤 명시적으로 활성화한다.

## Hermes 후속 연동

초기 계약 후보: portfolio snapshot, positions, cash, trades, performance, strategy explanation.
계좌의 사실과 전략 설명을 분리하고 source account mapping으로 중복을 제거한다.
수집 시점, 스키마 버전, 부분 실패, 인증을 정의한다. 명령/주문 endpoint와 DB 직접 공유는 피한다.
현재 Hermes API의 경로·구조·인증은 확인하지 않았으며 임의로 고정하지 않는다.
