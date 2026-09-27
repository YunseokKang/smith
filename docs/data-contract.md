# JSON 입력 계약 초안 v1

아래는 importer 구현을 위한 초안이다. 골격은 JSON을 DB에 수입하거나 검증하지 않는다.
가상 예시는 `examples/portfolio.example.json`을 참고한다.

## Envelope

- `schema_version`: `1`.
- `import_id`: 멱등성 키. 동일 ID/동일 payload는 no-op, 동일 ID/다른 payload는 오류.
- `source`: 입력 원천. API 원천과 수동 입력 원천은 구분한다.
- `mode`: `patch` 또는 `snapshot`.
- `as_of`: 원천 정보의 기준 시점. UTC offset이 있는 ISO 8601.
- `owners`: 이름 대신 불투명한 owner ID. 현재는 본인 하나만 사용.
- `records`: 종류별 레코드. `id`는 가격·이름 변화와 무관하게 안정적으로 유지.

## 레코드

공통: `id`, `kind`, `owner_id`, `effective_at`, `revision`, `status`.
`revision`으로 오래된 수정의 덮어쓰기를 방지한다. `status=closed`는 명시적 종료이며
물리 삭제하지 않는다. 공동 소유 확장 시 소유 지분 테이블과 합계 1 검증을 추가한다.

| kind | 필수 정보 초안 |
|---|---|
| asset | category, currency, value, valuation_method, liquidity |
| liability | currency, principal, annual_rate, maturity, repayment_method |
| cashflow | direction, currency, amount, frequency, start_date, end_date |
| goal | currency, target_amount, target_date, priority |

금액과 이율은 JSON number가 아닌 decimal string. asset/liability의 금액은 음수 금지,
부채 차감은 계산 계층에서 처리. 통화는 명시하며 원화 환산을 원본에 덮어쓰지 않는다.
보험 보장금액, 해약환급금, 월 보험료는 서로 다른 의미로 모델링한다.

## 갱신 규칙

- `patch`: 포함한 ID만 갱신. 누락된 ID는 유지.
- `snapshot`: 반드시 명시한 owner/source 범위만 교체 대상으로 삼는다. 다른 원천은 보존.
- snapshot에서 누락된 항목 종료는 미리보기 diff와 명시적 적용 절차를 거친다.
- 실제 변경의 `effective_at`과 시스템 기록 시점 `recorded_at`을 별도로 저장한다.
- 과거 정정은 `change_type=correction`, 정정 대상 revision과 이유를 기록한다.
- 입력 전체를 검증한 뒤 transaction으로 적용하며 부분 실패 시 반영하지 않는다.
- 알 수 없는 필드·버전, 중복 ID, 소유자 미정의, 잘못된 날짜·통화·금액은 거절한다.

예시는 최소 흐름 확인용이며 JSON Schema 완성본이 아니다. 실제 importer 단계에서 종류별
스키마와 테스트를 함께 구현한다. 사용자 실제 자료는 `data/` 등 Git 제외 경로에 둔다.
