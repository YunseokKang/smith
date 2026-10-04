# JSON 입력 계약 v1

`smith import`가 구현한 수동 입력 계약이다. 구현: `src/smith/importer.py`(검증),
`src/smith/ledger.py`(저장·이력). 가상 예시는 `examples/portfolio.example.json`.
사용자 실제 자료는 Git에서 제외된 `data/`에 둔다.

## 파일 형식

- UTF-8 JSON. Windows 편집기가 붙이는 BOM은 허용한다.
- 금액·이율·수량은 **decimal 문자열**(`"1000000"`, `"0.045"`). JSON 숫자에 소수점·지수가 있으면 거절한다.
  정수부 최대 15자리, 소수부 최대 8자리. 천 단위 구분 쉼표는 쓰지 않는다.
- 같은 객체 안의 중복 키, 알 수 없는 필드는 거절한다.
- 금액에는 부호를 쓰지 않는다. `-0`처럼 0에 붙은 음수 부호도 거절한다.
- 통화는 Smith가 평가할 수 있는 목록만 허용한다: `KRW`, `USD`, `EUR`, `JPY`, `CNY`.
  늘릴 때는 환율 출처와 함께 추가한다.
- 시각은 UTC offset이 있는 ISO 8601(`2026-09-01T09:00:00+09:00`), 날짜는 `YYYY-MM-DD`.
- ID(`import_id`, owner `id`, record `id`)는 1~64자의 영문·숫자·`.`·`_`·`-`. 이름·계좌번호를 넣지 않는다.
- 문제는 한 번에 모두 보고하며, 하나라도 있으면 아무것도 저장하지 않는다.

## Envelope

| 필드 | 규칙 |
|---|---|
| `schema_version` | `1` |
| `import_id` | 멱등성 키. 같은 ID·같은 내용은 no-op, 같은 ID·다른 내용은 오류. 거절된 import의 ID는 다시 쓸 수 있다 |
| `source` | `manual` 또는 `manual-<소문자·숫자>`. API 원천(예: 토스)은 어댑터 코드만 기록한다 |
| `mode` | 파일은 `patch`만. `snapshot`은 API 어댑터(`smith toss sync`)만 사용한다 |
| `as_of` | 원천 정보의 기준 시점. 가져오는 시각보다 미래면 거절 |
| `owners` | `[{"id": "..."}]`. 레코드의 `owner_id`는 여기에 선언돼야 한다. 본인은 `self`, 함께 관리하는 배우자(예정자 포함)는 `partner`를 쓴다. 토스 동기화 기본값은 `self` |
| `records` | 1개 이상. 한 import 안에서 같은 record `id`는 한 번만 |

`patch`는 포함한 레코드만 갱신하고 빠진 레코드는 그대로 둔다.
내용 비교는 JSON 서식과 키 순서를 무시한다.

**snapshot**(API 어댑터 전용)은 배치를 해당 owner·source의 활성 레코드 전체로 본다.
- revision은 원장이 매긴다. 필드가 최신 revision과 같으면 `unchanged`, 다르면 새 revision.
- 배치에 없는 활성 레코드는 종료 후보다. `--close-missing`이 없으면 배치 전체를 거절한다.
- 공급자가 다시 보고한 종료 레코드(예: 다시 매수한 종목)는 같은 ID로 재개할 수 있다.
  수동 입력의 "종료는 최종 상태" 규칙의 유일한 예외다.
- 매수 가능 금액·환율 같은 참고 지표는 레코드가 아닌 관측값(observations)으로 저장하며 순자산에 들어가지 않는다.

## 레코드 공통 필드

| 필드 | 규칙 |
|---|---|
| `id` | 가격·이름이 바뀌어도 유지하는 안정 ID |
| `kind` | `asset`, `liability`, `cashflow`, `goal`. 한 ID의 kind·owner·source는 바꿀 수 없다 |
| `owner_id` | 선언된 owner |
| `effective_at` | 이 revision 내용이 실제로 유효해지는 시각 |
| `revision` | 새 레코드는 1, 이후 정확히 직전 + 1 |
| `status` | `active` 또는 `closed`. 종료는 삭제가 아니다 |
| `change_type` | `update`(기본) 또는 `correction` |
| `corrects_revision` | correction일 때 필수. 정정 대상인 이전 revision |
| `reason` | correction일 때 필수, 그 외 선택. 200자 이하. 개인정보를 쓰지 않는다 |

`closed` revision은 종류별 필드를 생략할 수 있다. 적은 값은 검증한다.

## 종류별 필드

금액 필드는 음수를 허용하지 않는다. 부채 차감은 계산 계층이 한다.
원화 환산값을 원본에 덮어쓰지 않는다.

**asset**: `category`, `account_type`, `currency`, `value`(현재 평가액), `valuation_method`, `liquidity`

자산 배분과 세제·인출 제한을 모두 잃지 않도록 세 축을 분리한다.
예: 연금저축 계좌 안의 주식형 펀드는 `category=fund`, `account_type=pension_savings`.
- `category`(경제적 자산 유형): `cash`(입출금), `deposit`(예금), `installment_savings`(적금), `stock`,
  `fund`, `bond`, `real_estate`, `lease_deposit`(임차보증금), `insurance_surrender_value`(해약환급금),
  `crypto`, `unclassified`(구성 미상 총액), `other`
- `account_type`(계좌·세제 포장): `bank`, `brokerage`, `isa`, `pension_savings`(연금저축), `irp`, `dc`,
  `insurance`, `crypto_exchange`, `none`(부동산·보증금처럼 계좌가 없음), `other`
- 선택 필드(종목 단위 세부): `symbol`(영문·숫자·`.`·`-` 20자 이하), `instrument_name`(100자 이하),
  `market`(`KR`, `US`, `other`), `quantity`, `unit_price`, `average_cost`, `value_after_costs`
  (세금·수수료 공제 후 평가액). 토스 동기화가 채우며 수동 입력에도 쓸 수 있다.
- 선택 필드 `occupancy`(부동산 용도): `owner_occupied`(실거주), `leased_out`(임대), `vacant`, `other`.
- 구성을 모르는 계좌 총액은 `unclassified` 한 건으로, 구성을 알면 자산군별 레코드로 나눠 입력한다.
  한 계좌를 두 방식으로 동시에 입력하면 이중 합산되므로, 나눌 때는 총액 레코드를 종료한다.
- `valuation_method`: `manual`(본인 추정), `statement`(금융기관 앱·명세서 값), `market`(시세×수량),
  `appraisal`(KB 시세 등 외부 평가), `api`
- `liquidity`: `immediate`(즉시 사용), `days`(수일 내 현금화), `months`(수개월 필요),
  `restricted`(인출 제한·불이익)
- 보험 보장금액은 자산이 아니다. 월 보험료는 cashflow(`insurance_premium`)로 입력한다.
  보장 적정성 자문이 필요해지면 순자산에 합산하지 않는 별도 coverage 레코드를 추가한다(미구현).
- 국민연금 예상 수령액은 자산이 아니라 cashflow(`pension_income`)로 입력한다.

**liability**: `category`, `currency`, `outstanding_principal`(현재 잔여 원금), `annual_rate`,
`rate_type`, `maturity`(선택), `repayment_method`, `credit_limit`(선택), `collateral_record_id`(선택)
- `category`: `mortgage`, `jeonse_loan`, `credit_loan`, `credit_line`(마이너스통장), `card_balance`,
  `policy_loan`(보험계약대출), `lease_deposit_obligation`(임대보증금 반환 의무), `other`
- `credit_limit`: 마이너스통장 등의 한도. 부채 원금이 아니며 순자산에 들어가지 않는다.
- `collateral_record_id`: 담보 자산 레코드 ID(예: 보험계약대출의 해약환급금, 주택담보대출의 부동산).
- `annual_rate`: 소수 비율. `0.045`가 4.5%. 1 이상이면 거절한다(퍼센트 입력 실수 방지).
- `rate_type`: `fixed`, `variable`, `mixed`, `unknown`(미확인)
- `repayment_method`: `bullet`(만기일시), `equal_payment`(원리금균등), `equal_principal`(원금균등),
  `revolving`, `other`, `unknown`(미확인). 미확인 값은 확인되면 다음 revision으로 갱신한다.

**cashflow**: `direction`, `category`, `currency`, `amount`(0 초과), `frequency`, `start_date`, `end_date`(선택),
`liability_record_id`(`loan_payment`일 때 필수, 그 외 금지)
- `loan_payment`는 상환할 부채 레코드와 연결한다. 원금·이자 분리는 연결된 부채의 이율·상환 방식으로
  계산 단계에서 추정하며, 추정임을 표시한다.
- 유입 category: `salary`, `business_income`, `rental_income`, `pension_income`, `investment_income`,
  `other_income` → `direction=inflow`
- 유출 category: `living_expense`, `housing_cost`, `loan_payment`, `insurance_premium`, `tax`,
  `education`, `other_expense` → `direction=outflow`
- `internal_transfer`: 본인 계좌 간 이동(적금 납입 등). 수입·지출로 계산하지 않는다.
- `frequency`: `once`, `monthly`, `quarterly`, `annual`. `once`는 `end_date`를 쓰지 않는다.
- `end_date`는 `start_date` 이후여야 한다. 없으면 계속 유지되는 흐름이다.

**goal**: `category`, `currency`, `target_amount`(0 초과), `target_date`, `priority`
- `category`: `home`, `emergency_fund`, `retirement`, `education`, `major_purchase`, `debt_repayment`, `other`

**참조 필드**(`collateral_record_id`, `liability_record_id`)는 원장에 있거나 같은 import에 있는
해당 kind 레코드를 가리켜야 한다.
- `priority`: `high`, `medium`, `low`

## 분류 변경 정책

현재 분류는 초기 기준이며 실제 데이터를 넣으며 바뀔 수 있다. 변경 비용을 낮추기 위해 다음을 지킨다.

- 분류 값과 참조 규칙은 `src/smith/records.py` 한 곳에서만 정의한다.
- 종류별 필드는 원장에 JSON으로 저장하고, 저장된 값은 읽을 때 다시 검증하지 않는다.
  분류가 바뀌어도 과거 revision을 그대로 읽을 수 있다.
- **값 추가**: 목록에 추가만 한다. 원장 migration이 필요 없다.
- **선택 필드 추가**: 기존 입력과 호환된다. 기존 revision에는 없는 값(None)으로 취급한다.
- **이름 변경·병합**: 저장된 revision을 고치지 않는다. 입력은 새 이름만 받고, 계산 계층이
  읽을 때 옛 값을 새 값으로 매핑하는 별칭 표를 둔다.
- **삭제**: 입력에서만 막는다. 계산 계층은 알 수 없는 분류 값을 만나도 중단하지 않고
  `other`로 집계하면서 경고와 함께 표시한다(2단계에서 구현).
- 필수 필드 추가나 기존 필드의 의미 변경처럼 호환되지 않는 변경은 `schema_version`을 올린다.

## revision과 이력 규칙

- revision은 추가만 한다. 기존 revision은 수정·삭제하지 않는다.
- 같은 revision 번호로 같은 내용을 다시 넣으면 `unchanged`, 다른 내용이면 오류.
- 직전 + 1이 아닌 revision(오래된 값, 번호 건너뜀)은 오류.
- 각 revision에는 입력의 `effective_at`과 시스템이 기록한 `recorded_at`을 따로 저장한다.
- **시점 T의 상태**: correction으로 대체되지 않은 revision 중 `effective_at`이 T 이하인 가장 늦은 것.
  같은 시각이면 revision 번호가 큰 쪽.
- **update·종료는 시간이 앞으로만 간다**: `effective_at`이 현재 상태 revision의 `effective_at`보다
  늦어야 한다. 과거 값의 오류는 correction으로만 고친다.
- **종료는 최종 상태다**: `closed` 이후에는 correction만 허용한다. 다시 시작하는 자산은 새 record ID를 쓴다.
  잘못된 종료는 종료 revision을 정정해 되돌린다.
- **correction**: 대상 revision을 그 기간에 대해 대체하므로 `effective_at`이 대상과 같아야 한다.
  이후의 실제 변경(update)은 그대로 유지된다.
  예: rev1(9/1, 100) → rev2(10/1, 200) → rev3(rev1 정정, 9/1, 150)이면 9월은 150, 10월 이후는 200.
  이미 정정된 revision은 다시 정정할 수 없고, 최신 correction을 정정한다.
  revision의 `effective_at` 자체가 틀린 경우의 정정은 아직 지원하지 않는다.
- **update와 correction의 구분**: update는 실제 변동, correction은 과거 입력 오류다.
  보고서의 자산 변동 분해에서 correction은 실제 변동으로 계산하지 않는다.
- `known_at`으로 조회하면 그 시각까지 기록된 revision만 사용한다. 과거 보고서를 같은 입력으로 재현한다.

## 명령

```powershell
.\.venv\Scripts\smith.exe import data\2026-10.json --dry-run   # 계획만 표시, 저장 안 함
.\.venv\Scripts\smith.exe import data\2026-10.json             # 원자적으로 적용
.\.venv\Scripts\smith.exe records --as-of 2026-10-15T00:00:00+09:00
.\.venv\Scripts\smith.exe records --as-of 2026-10-15T00:00:00+09:00 --known-at 2026-10-20T00:00:00+09:00
```

기본 원장 경로는 `data/smith.db`(`--db`로 변경). `--dry-run`과 `records`는 원장을 읽기 전용으로 열며
파일을 만들거나 스키마를 쓰지 않는다. `--known-at`은 그 시각까지 기록된 revision만 사용해
이후 입력된 정정을 제외한 당시의 관점을 재현한다.

## 아직 없는 것

- 공동 소유 지분(소유 지분 테이블과 합계 1 검증).
- 보험 보장(coverage) 레코드.
- 보고서가 사용한 import·snapshot ID 저장(6단계에서 `--known-at`과 함께 재현 근거로 사용).
