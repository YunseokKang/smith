# BUG-2026-002 수정 병합이 거부된 종목 의견 자리를 다른 종목으로 채워 1위 종목이 "의견 없음"으로 발송됨

| 항목 | 내용 |
|---|---|
| 발생 | 2026-10-17 화요 투자·시장 보고(실발송), 2026-10-11 월요 보고에서도 같은 경로로 재현 가능 |
| 영향 | 비중 1위이자 회사 주식인 종목의 "일부 정리" 의견이 모델 출력에 있었는데도 보고서 종목 표에 "의견 없음"으로 나갔고, 소액 종목에는 상세 카드가 붙었다. 고객 판단에 가장 중요한 의견이 빠졌다 |
| 수정 | `narrative.merge`에 첫 호출의 원출력(`raw`)을 넘겨 거부된 목록 항목을 **정체(심볼·제안 키·제목)**로 맞춘다. `must_opine` 종목의 의견 누락은 거부 사유로 요구하고, 끝까지 없으면 `code_opinion`이 채운다(report-design §17.4) |

## 재현

1. 첫 호출 출력: `holdings[0]`=삼성전자 trim(거부: unknown-ref), `[1]`=VOO add(통과), `[2]`=QQQ hold(거부: ungrounded-number), `[9]`=INTC exit(거부).
2. 수정 호출 출력에서 QQQ·086900 등이 통과, 삼성전자는 다시 거부(unofficial-law-source).
3. 종전 `merge`: 거부 항목마다 "아직 들어가지 않은 수정본 첫 항목"을 채웠다. `holdings[0]`(삼성전자) 자리에 QQQ가, `holdings[2]`(QQQ) 자리에
   086900이 들어갔다. 삼성전자 항목은 "병합 해결"로 간주되어 `restore`가 복원하지 않았다.
4. 결과: 종목 표의 삼성전자가 "의견 없음".

감사 로그(`advice_runs`)의 `dropped`·`merged_dropped`·원출력의 심볼 목록만으로 확인했다(금액은 보지 않음).

## 원인

`merge`가 거부 경로(`holdings[i]`)의 정체를 알지 못했다. 수정본 목록에서 "아직 없는 첫 항목"을 고르는 규칙은 거부 항목이 하나일 때만
맞고, 여럿이 섞이면 자리를 바꿔 끼운다. 테스트는 거부 항목이 하나인 경우(`test_a_block_failing_twice_is_restored_from_the_output_its_path_indexes`)
만 다뤘다.

## 수정

- `merge(first, dropped, repaired, raw)`: `raw[field][i][identity]`로 원하는 정체를 정하고 같은 정체의 수정본만 받는다. 통찰·지켜볼 것은
  제목이 바뀔 수 있어 같은 위치의 새 항목을 대신 받는다. `missing-opinion` 항목은 심볼로 맞춘다.
- 함께 드러난 모순 둘을 고쳤다. ① 투자·시장판은 세금 입력이 없어 `tax`를 근거로 인정하지 않았는데, 프롬프트는 정리·매도 의견에 매도 세금
  순서를 요구했다 → 보유 종목이 있으면 `tax` 인정. ② 검증기가 "27억 2,500만 원"처럼 만 단위에 0이 붙은 반올림을 거부해(정밀도 5천 원) 모델이
  1원 단위 금액을 쓰게 몰았다 → 마지막 자리의 절반(최대 두 자리)을 허용.

## 검증

`tests/test_pb_upgrade.py::NarrativeRuleTests::test_the_repair_merge_matches_rejected_items_by_identity`(사건 그대로의 네 종목 시나리오와
누락 의견 병합), `CalculatorTests::test_the_employer_stock_gets_a_trim_schedule_and_never_reads_no_opinion`(누락 거부와 코드 의견).

## 예방

- 병합·복원처럼 모델 출력의 목록을 다루는 코드는 정체 키로만 맞추고, 테스트는 거부 항목이 둘 이상 섞인 경우를 포함한다.
- 보고서가 반드시 담아야 할 블록(한도 초과·회사 주식 의견)은 코드가 존재를 확인하고 없으면 결정적 대체를 만든다. 모델에만 맡기지 않는다.
- 프롬프트가 요구하는 내용과 검증기가 허용하는 근거가 어긋나지 않는지, 프롬프트 변경 때 `_known_refs`·`_LAW`를 함께 본다.
