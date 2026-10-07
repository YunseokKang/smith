# 토스증권 Open API 확인 결과

확인일: 2026-09-27. 근거: 공식 OpenAPI 명세 `토스증권 Open API` v1.2.17
(`https://openapi.tossinvest.com/openapi-docs/latest/openapi.json`)와 개요 문서
(`https://openapi.tossinvest.com/openapi-docs/overview.md`).
실제 계좌 호출로는 아직 검증하지 않았다. 한도·필드는 사전 공지 없이 바뀔 수 있으므로
어댑터 구현 시 명세 버전을 다시 확인한다.

## 인증

- OAuth 2.0 Client Credentials. `POST /oauth2/token`, `application/x-www-form-urlencoded`,
  `grant_type=client_credentials`, `client_id`, `client_secret`.
- 응답: `access_token`(JWT), `token_type=Bearer`, `expires_in`(초). 명세 예시는 86400초.
- refresh token이 없다. 만료되면 같은 엔드포인트로 재발급한다.
- **client당 유효한 access token은 1개다. 재발급하면 이전 token이 즉시 무효화된다.**
  같은 client를 다른 도구와 함께 쓰면 서로의 token을 끊는다.
- `client_id`/`client_secret`은 토스증권 WTS 설정 > Open API 메뉴에서 발급한다.
- **허용 IP 등록 필수.** 목록에 없는 IP에서 호출하면 403. 가정용 회선은 IP가 바뀔 수 있다.
- sandbox 환경은 문서에 없다. 운영 계좌에 바로 호출된다.

## 권한: 조회 전용 키가 없다

- `securitySchemes.oauth2ClientCredentials.flows.clientCredentials.scopes`가 `{}`다.
- 같은 token으로 주문 생성·정정·취소(`POST /api/v1/orders`, `.../modify`, `.../cancel`),
  조건주문 생성·수정·취소(`POST`/`DELETE /api/v1/conditional-orders...`)를 호출할 수 있다.
- 따라서 조회 전용 경계는 **Smith 어댑터 코드의 허용 목록**으로 강제한다.
  허용 목록 밖의 요청은 네트워크 호출 전에 거부하고 테스트로 검증한다.

## Smith가 사용할 엔드포인트

| 용도 | 요청 | 한도 그룹 | 비고 |
|---|---|---|---|
| token | `POST /oauth2/token` | AUTH 5/s | 인증 요청이며 금융 실행이 아니다 |
| 계좌 목록 | `GET /api/v1/accounts` | ACCOUNT 1/s | `accountSeq` 획득 |
| 보유 주식 | `GET /api/v1/holdings` | ASSET 5/s | 헤더 `X-Tossinvest-Account` |
| 매수 가능 금액 | `GET /api/v1/buying-power?currency=` | ORDER_INFO 6/s (09:00~09:10 KST 3/s) | 헤더 필요 |
| 환율 | `GET /api/v1/exchange-rate?baseCurrency=&quoteCurrency=` | MARKET_INFO 3/s | 참고용 표시 환율 |

한도는 client × API 그룹 단위다. 응답 헤더 `X-RateLimit-Limit`/`X-RateLimit-Remaining`,
429 응답의 `Retry-After`를 따른다.

## 응답 의미와 Smith 설계 반영

**계좌** (`GET /api/v1/accounts`)
- 현재 종합매매(`BROKERAGE`) 계좌만 반환한다. 연금저축(`PENSION_SAVINGS`)은 enum에만 있고
  노출되지 않는다. 연금저축은 JSON 수동 입력으로 관리한다.
- `accountNo`는 계좌번호 원문이다. 저장·로그·LLM payload에 넣지 않는다.
- `accountSeq`는 다른 API 헤더에 쓰는 식별 키다. 어댑터 내부에서만 쓰고, 원장에는
  불투명한 내부 계좌 ID로 매핑해 저장한다.

**보유 주식** (`GET /api/v1/holdings`)
- 국내(KR)·미국(US) 주식만 포함한다. 해외 옵션·채권은 제외된다.
- 금액·수량은 decimal 문자열이다. 종목별로 `symbol`, `name`, `marketCountry`, `currency`,
  `quantity`, `lastPrice`, `averagePurchasePrice`, `marketValue{purchaseAmount, amount,
  amountAfterCost}`, `profitLoss`, `dailyProfitLoss`, `cost{commission, tax|null}`.
- `lastPrice`·`averagePurchasePrice`의 통화는 문서에 명시돼 있지 않다. 저장된 실제 응답 값으로 종목 `currency` 기준임을
  확인했다(2026-10-07, `docs/data-contract.md` 종목 단위 필드 참고). `purchaseAmount`·`profitLoss`는 저장하지 않는다.
- 요약 금액은 통화별(`krw`, `usd`) 합이며 통화 간 환산 합산을 하지 않는다. 해외 종목이 없으면 `usd`는 null.
- **응답에 평가 기준 시각이 없다.** Smith는 수집 시각을 기록하고, 평가 시각은
  "수집 시각 기준 근사"로 표시한다.
- 평가금액(`amount`)과 세금·수수료 공제 후 금액(`amountAfterCost`)을 구분해 저장한다.

**현금** (`GET /api/v1/buying-power`)
- 예수금·출금 가능액 조회 API는 없다. `cashBuyingPower`는 "미수 미발생 기준 현금 기반 매수 가능 금액"이다.
- Smith는 이 값을 예수금이나 출금 가능액이 아닌 **매수 가능 금액**으로 표시한다.
  토스 앱의 예수금·출금 가능액과 차이가 날 수 있다(AC-01에서 대조).
- **자산 `value`로 저장하지 않는다.** 계좌의 참고 지표로 따로 저장해 이중 합산을 막는다.
  따라서 토스 계좌의 현금 잔액은 API로 수집되지 않는다. 수동 입력하거나 "미수집"으로 표시한다.
- KRW는 정수(원), USD는 소수(달러)다.

**환율** (`GET /api/v1/exchange-rate`)
- KRW↔USD. `rate`(매수 환율)와 `midRate`(매매기준율), 유효 구간 `validFrom`~`validUntil`(보통 1분).
- 참고용 표시 환율로, 실제 거래 환율과 다를 수 있다.
- 평가 환산은 `midRate`를 쓰고 출처·유효 시작 시각을 함께 저장한다.

**enum**: `Currency`(KRW, USD), `MarketCountry`(KR, US)는 새 값이 추가될 수 있다고 명시돼 있다.
Smith는 값의 성격에 따라 다르게 처리한다.
- 금액 평가에 영향을 주는 값(지원하지 않는 통화, 누락·음수 금액, 0 이하 환율, 시간대 없는 환율 시각)은
  동기화 전체를 중단한다. 부분 수집은 빠진 종목을 매도된 것처럼 보이게 하기 때문이다.
- 금액을 바꾸지 않고 보존할 수 있는 값(알 수 없는 `MarketCountry`)은 `other`로 저장한다.
- 응답 구조 오류(`result`·`items` 누락, 목록이 아님, 필수 필드 누락, 중복 종목)도 전체 중단한다.
- DNS·timeout·연결 오류는 `network-error`, 성공 응답의 구조 오류는 `invalid-response` 코드로만 보고한다.

## 오류

- 형식: `{"error": {"requestId", "code", "message", "data"}}`. `requestId`는 헤더 `X-Request-Id`와 같다.
- 주요 코드: `expired-token`/`invalid-token`(401), `account-header-required`(400),
  `rate-limit-exceeded`/`edge-rate-limit-exceeded`(429), `internal-error`/`maintenance`(500).
- 로그에는 HTTP 상태, `code`, `requestId`만 남긴다. `message`와 `data`는 남기지 않는다.

## Smith 구현 현황

- `src/smith/toss.py`: 표준 라이브러리 `urllib` 기반 조회 전용 client. 위 표의 5개 요청만 허용하고,
  그 밖의 요청은 네트워크 전에 `ForbiddenRequest`로 거부한다. 요청 timeout 10초.
- token은 실행마다 발급해 메모리에만 두고, `expired-token`/`invalid-token`이면 한 번 재발급한다.
  OS 자격 증명 저장소(Windows Credential Manager)는 값 길이 제한이 있어 JWT 저장에 쓰지 않는다.
  **같은 client를 다른 도구와 함께 쓰면 Smith 실행 때마다 그 도구의 token이 끊긴다.** Smith 전용 client를 권장한다.
- `client_id`/`client_secret`은 `src/smith/credentials.py`가 OS 자격 증명 저장소(서비스 `smith.toss`)에 둔다.
  같은 Windows 사용자로 실행되는 프로세스는 읽을 수 있으므로, 5단계 headless 자문 프로세스에는
  shell·임의 코드 실행 도구를 주지 않는다.
- `smith toss check`: token, 계좌 목록, 계좌별 보유 주식·매수 가능 금액(KRW, USD), USD/KRW 환율을 한 번씩 조회한다.
  기본 출력은 개수·통화·시각만 보여 개발 에이전트와 공유해도 되는 수준이다. 계좌번호는 어떤 모드에서도 출력하지 않는다.
- `smith toss sync [--dry-run] [--close-missing] [--show-values]`(`src/smith/toss_sync.py`): 위 요청을 모두
  성공해야 snapshot 배치를 만든다. 하나라도 실패하거나 지원하지 않는 통화가 오면 아무것도 저장하지 않는다
  (부분 수집은 빠진 종목을 매도된 것처럼 보이게 하므로).
  - 계좌 키: `toss-` + `accountSeq` 해시 8자리. 계좌번호는 버린다.
  - 종목 레코드 ID: `<계좌 키>-<symbol 소문자>`. `category=stock`, `account_type=brokerage`,
    `liquidity=days`, `valuation_method=api`, 수량·현재가·평균단가·공제 후 평가액을 선택 필드로 저장.
  - 평가 시각이 응답에 없어 `effective_at`은 수집 시각이다.
  - 매수 가능 금액(KRW, USD)과 USD/KRW `midRate`는 관측값으로 저장한다. 환율의 관측 시각은 `validFrom`.
  - 기본 출력은 개수만 보인다. `--show-values`일 때만 레코드 ID(종목 심볼 포함)를 나열한다.
  - 실제 실행(미리보기 제외)은 성공·실패를 원장 `sync_runs`에 오류 코드와 함께 남긴다.
- 2026-10-04 실제 계좌로 동기화 확인: 18종목 생성, 즉시 재실행 시 18종목 unchanged.

## 실제 계좌 연결 확인 (2026-10-04)

`smith toss check`(값 숨김 모드)로 확인했다.
- token 발급 성공, `expires_in`은 약 86400초(24시간).
- 계좌 목록: `BROKERAGE` 1개.
- 보유 주식·매수 가능 금액(KRW, USD)·USD/KRW 환율 모두 정상 응답. 보유 종목에 KRW와 USD가 함께 있다.
- AC-01: 사용자가 `--show-values` 출력을 토스 앱과 대조해 일치함을 확인했다.
- 이 client는 Smith 전용이다(다른 도구와 공유하지 않음). 실행마다 token을 발급하는 현재 방식을 유지한다.

## 아직 확인하지 않은 것

- 한 사용자가 client를 여러 개 발급받을 수 있는지.
- 매수 가능 금액과 앱의 예수금·출금 가능액·결제 예정 금액의 관계.
- 보유 주식 `lastPrice`의 지연 여부와 장 마감 후 값의 의미.
