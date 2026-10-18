# BUG-2026-003 출처 확인 해시가 전화번호 규칙에 걸려 서술 없이 발송

- 발생: 2026-10-18 04:25 UTC, 수동 재발송 `smith report send-now --kind rates`(run 5194ccaa).
- 영향: 금리·대출판이 AI 서술(판단·방향·통찰) 없이 계산 결과만으로 발송됐다. 다른 판은 영향 없었으나 같은 결함은 어떤 판에서든
  무작위로 재발할 수 있었다(아래 원인).
- 재현: 그 보고서의 브리프(`9acb1a0f…`)로 `narrative.build_payload`를 만든 뒤 `check_outbound`를 호출하면 `PayloadRejected(['phone'])`.
  항목 R8의 어느 본문 필드도 단독으로는 걸리지 않고, 항목 전체를 직렬화했을 때만 걸린다.

## 원인

출처 확인(§5차 리뷰, 2026-10-05)이 조사 항목마다 `check` 기록(최종 URL, HTTP 상태, 본문 SHA-256, 대조한 숫자)을 붙여 저장한다.
서술 단계는 브리프 항목을 그대로 `items_untrusted`로 모델 입력에 넣었고, 그 안의 64자 16진수 해시에 숫자가 10~11개 연달아 나오면
전화번호 규칙 `01[016789]-?\d{3,4}-?\d{4}`에 걸린다. 16진수 한 글자가 숫자일 확률이 10/16이라 항목 수십 개 중 하나는 걸리기 쉽다.
`check_outbound`는 모델 호출 전에 실행되므로 호출 기록(advice_runs)도 남지 않아, 발송 로그에는 조사 성공 2초 뒤 발송만 찍혔다.

## 수정

- `narrative._model_items`: 모델에 보내는 조사 항목은 본문 필드(ref, topic, headline, detail, published_on, publisher, source_title,
  source_url, kind, tier)만 담는다. 확인 기록은 저장소에 남아 푸터·시스템 노트의 "출처 확인 n건" 집계에만 쓴다.
- 테스트: 해시에 전화번호꼴 숫자열이 든 항목을 가진 브리프로 `build_payload` → `check_outbound`가 통과하고, 모델 입력에 `check`가
  없음을 확인(`tests/test_v3_upgrade.py`).

- 1차 리뷰(2026-10-18) 후속: 같은 경로가 `source_url`에도 남아 있었다(`…/01012345678` 같은 URL). 모델 입력 항목에서 `source_url`을
  빼고, 공식 발표 링크(`announcements.link`)와 통계 출처(`evidence.source`)도 모델 입력에서 뺐다(`narrative._without_links`). 링크는
  저장된 브리프와 보고서 데이터에서 렌더링한다.

## 검증·예방

- 검증: 재현 스크립트가 수정 뒤 통과. 금리판은 수정본으로 재발송.
- 기존 테스트가 놓친 이유: 브리프 픽스처의 항목에 `check` 기록이 없었고, 식별정보 검사는 본문 필드만 상정했다.
- 예방: 모델 입력은 허용 목록 필드만 보내고(원칙: 최소 페이로드), 저장용 메타데이터를 모델 입력에 섞지 않는다. 서술이 호출 전
  거부되면 거부 규칙을 발송 로그에 남긴다(`narrate`의 실패 코드를 `_say`로 기록, 후속).

## 참조

- `src/smith/narrative.py` `_full_payload`, `src/smith/payload.py` `_PII_PATTERNS`, `src/smith/sources.py` `check`.
- 관련: BUG-2026-002(수정 병합), docs/report-design.md §10.4(조사 ↔ 자문 분리).
