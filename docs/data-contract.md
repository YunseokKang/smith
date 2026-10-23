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
  (세금·수수료 공제 후 평가액). 토스 동기화가 채우며 수동 입력에도 쓸 수 있다. `unit_price`(현재가)와
  `average_cost`(평균 매입 단가)는 **1주당, 그 자산의 `currency` 기준**이다(미국 주식이면 달러). 수익률
  `(unit_price − average_cost) / average_cost`는 환율 없이 계산하고, 원화 미실현 손익만 환율이 있을 때 계산한다.
  토스 값의 통화는 2026-10-07 저장된 실제 데이터로 확인했다: 원화·달러 모든 종목에서 `value = quantity × unit_price`가
  정확히 맞고, `average_cost / unit_price`는 0.24~3.1배여서 통화가 섞였다면 나왔을 약 1,400배 차이가 없다.
- 선택 필드 `occupancy`(부동산 용도): `owner_occupied`(실거주), `leased_out`(임대), `vacant`, `other`.
- 선택 필드 `managed_by`(운용 주체): `self`, `hermes`(별도 fund manager가 운용), `other`.
- 선택 필드 `region`(부동산 소재 지역, 시·구 수준): 행정구역 단위만 받는다. 시·도(…특별시·광역시·특별자치시·도)는
  선택, 이어서 시, 마지막은 시·군·구로 끝나야 한다(예: "○○도 ○○시 ○○구", "○○특별시 ○○구"). 동·읍·면·리,
  아파트명, 도로명·번지·동호수는 받지 않는다. 지역 정책·시세 조사 주제와 자문 맥락에 쓴다(사용자 승인 2026-10-05).
  자문은 Hermes 운용 자산의 직접 매각·변경을 권하지 않는다.
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
- `credit_limit`: 마이너스통장 등의 한도. 부채 원금이 아니며 순자산에 들어가지 않는다. 잔액이 0이면 이자가 없는 미사용
  한도로 보고 상환 제안 대상에서 빼며, 보고서는 유동성 여유로 적는다(report-design §18.4).
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
  `other_income`, `bonus`, `stock_compensation` → `direction=inflow`
- 유출 category: `living_expense`, `housing_cost`, `loan_payment`, `insurance_premium`, `tax`,
  `education`, `other_expense` → `direction=outflow`
- `internal_transfer`: 본인 계좌 간 이동(적금 납입 등). 수입·지출로 계산하지 않는다.
  선택 필드 `target_record_id`로 입금되는 자산 레코드(예: 연금저축 계좌의 펀드)를 가리키면, 계좌 유형별 연간
  납입액(세액공제 한도 계산)에 쓴다. `internal_transfer`에만 쓸 수 있다.
- `frequency`: `once`, `monthly`, `quarterly`, `annual`. `once`는 `end_date`를 쓰지 않는다.
- `end_date`는 `start_date` 이후여야 한다. 없으면 계속 유지되는 흐름이다.
- `commitment`(선택): `fixed`(계약상 의무) 또는 `discretionary`(추가 원금 상환처럼 언제든 멈출 수 있음).
  긴급 자금 조달처럼 지출 조정 여지를 따질 때 쓴다. 없으면 미확인이다.
- `pension_source`(선택, `pension_income`만): `national`(국민연금), `occupational`(퇴직연금), `insurance_annuity`(변액·연금보험),
  `pension_savings`(연금저축), `irp`, `other`. 연금 스택(report-design §18.5)의 파이프라인 구분이다. `amount`는 현재 가치의 월 예상
  수령액, `start_date`는 수령 개시일(미래 가능)이며, 개시 전 흐름은 월 현금흐름에 들어가지 않는다. `amount_basis`를 `net`으로 두면
  기관이 알려 준 세후 금액으로 보고 세금을 다시 빼지 않는다(기본 `gross`). `price_basis`(선택): `today`(기본, 현재 가치) 또는
  `nominal`(보험사 예시표처럼 수령 첫해의 명목 금액). 명목 금액은 연금 스택이 물가 2%로 현재 가치로 환산해 쌓는다.
  `contribution_months`·`contribution_end`(선택, 함께, `pension_income`만): 국민연금 예상액이 전제한 가입 개월 수(정수)와 납부 종료일.
  은퇴 나이별 연금 스택(report-design §30)이 조기 은퇴로 줄어드는 가입 개월 수에 비례해 감액한다. 없으면 조회값을 그대로 쓰고 상한이라고 밝힌다.
- `policy_record_id`(선택, `insurance_premium`·`pension_income`만): 그 보험료가 들어가거나 연금이 나오는 보험 계약(해약환급금 asset
  레코드 ID). 보험 계약이 여러 건이면 이 연결이 있어야 상품 평가(report-design §19)가 계약별로 나온다. 연결이 없고 연금·보험료·
  해약환급금이 각각 하나뿐이면 그 셋을 한 계약으로 본다. 그 밖에는 평가를 보류하고 "다음에 확인할 것"이 연결을 요청한다.
- **예정 소득(FR-20)**: 유입 category `bonus`(성과급·상여)와 `stock_compensation`(주식 보상: PSU·RSU·자사주 성과급 등).
  - 지급 한 번이 레코드 하나다(`frequency=once` 필수). 매년 받는 성과급도 연도별로 따로 입력한다.
  - `certainty`(필수): `confirmed`(통지·계약으로 지급과 금액(또는 주식 수)이 확정, 재직 같은 통상 조건만 남음), `expected`(지급은
    거의 확실하고 금액만 실적·주가에 따라 달라짐), `possible`(지급 자체가 아직 정해지지 않은 조건에 달림: 주총 승인, 주가 기준 미달 시
    0 등). 다른 유입 category에도 선택으로 쓸 수 있고, 없는 일회성 유입은 `possible`로 본다.
    사용자 결정(2026-10-17): 합의·계약으로 지급이 정해졌고 남은 조건이 지금 충족돼 있거나 통과 가능성이 높은 보상(예: 노사 합의로
    지급 일정이 공표되고 주총 승인만 남은 성과급, 현재 주가가 이미 지급 기준을 넘은 주가 연동 보상)은 고객이 `expected`로 둘 수 있다.
    그 판단과 남은 조건은 레코드 `reason`에 남기고, 불리 시나리오에서는 빠진다.
  - `amount_basis`(필수): `gross`(세전) 또는 `net`(세후 실수령). 이 두 category에만 쓴다. `net`이면 세전 금액은 미상으로 두며,
    같은 해 그 뒤에 받는 보상의 세율 구간도 정할 수 없어 세후가 미상이 된다. 세후로 입력한 주식 보상의 `quantity`는 세금 뒤 실제로
    받을 주식 수다.
  - `stock_compensation`은 `symbol`, `market`, `quantity`(지급 주식 수)가 필수다. `amount`는 입력 시점의 원화 추정액이며,
    원장에 같은 시장·심볼·통화의 보유 종목 단가가 있으면 Smith가 가장 최근 단가 × 수량으로 다시 평가한다.
  - 원화 금액으로 정해져 지급일 주가로 자사주를 받는 성과급은 `bonus`에 `symbol`·`market`(둘 다)을 적는다(`quantity`는 쓰지 않음).
    같은 종목 집중도에 들어가고, 지급일부터 주가 위험을 진다고 본다. 매도 제한이 단계적이면 금액을 나눠 레코드마다 `available_from`을 둔다.
  - `available_from`(선택): 의무 보유 등으로 실제로 쓸 수 있게 되는 날(지급일 이후). 없으면 지급일.
  - 받기 전까지는 순자산·월 현금흐름에 넣지 않는다. 계산 규칙은 `src/smith/future_income.py`.
  - **지급 수명주기**: 지급 여부는 날짜나 종료 여부로 추정하지 않고 고객이 알려 준 사실로만 정한다.
    - `payment_status`(선택, 두 category만): `scheduled`(기본) 또는 `received`.
    - 받았으면 다음 revision으로 `payment_status=received`, `received_on`(실제 지급일), `taxable_amount`(실제 세전 과세 금액,
      그 레코드 통화)를 함께 적는다. 이 둘은 `received`일 때만 쓰고, 그때는 필수다. 받은 돈은 현금·주식 자산으로 따로 갱신한다.
    - 받은 레코드는 **종료하지 않는다.** 계획에서는 빠지고, 같은 해 뒤에 받는 보상의 세율 구간 계산에만 실제 과세 금액으로 쓰인다.
      종료(`closed`)는 지급이 취소되거나 받을 자격을 잃었다는 뜻이다.
    - 예정일이 지났는데 `received`가 아니면 "지급 확인 필요"(overdue)다. 목록에는 남지만 모든 계획에서 빠지고, "다음에 확인할 것"에
      꼭 필요 요청(`pay-overdue`)이 뜬다. 미뤄졌으면 `start_date`를 갱신한다.

**goal**: `category`, `currency`, `target_amount`(0 초과), `target_date`, `priority`
- `category`: `home`, `emergency_fund`, `retirement`, `education`, `major_purchase`, `debt_repayment`, `other`

**참조 필드**(`collateral_record_id`, `liability_record_id`, `target_record_id`)는 원장에 있거나 같은 import에 있는
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
