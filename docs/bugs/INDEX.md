# 운영 결함 기록

| ID | 제목 | 발생 | 수정 |
|----|------|------|------|
| [BUG-2026-001](BUG-2026-001-mail-follow-up-without-context.md) | 메일 질의응답의 이어지는 질문이 앞선 대화를 모른 채 답함 | 2026-10-05 | 원장 v16 대화 이력 |
| [BUG-2026-002](BUG-2026-002-repaired-opinion-took-another-holdings-slot.md) | 수정 병합이 거부된 종목 의견 자리를 다른 종목으로 채워 1위 종목이 "의견 없음"으로 발송됨 | 2026-10-17 | `narrative.merge` 정체 기반 병합, `must_opine` 강제 |
| [BUG-2026-003](BUG-2026-003-source-check-hash-tripped-the-phone-rule.md) | 조사 항목의 출처 확인 해시(SHA-256) 숫자열이 전화번호 규칙에 걸려 금리판이 서술 없이 발송 | 2026-10-18 | 모델 입력 항목을 본문 필드로 한정, 서술 실패를 발송 로그에 기록 |
| [BUG-2026-004](BUG-2026-004-fitbit-days-booked-on-utc-date.md) | Fitbit 수면·운동 기록이 현지 날짜가 아니라 UTC 날짜로 저장돼 아침에 끝난 잠이 전날로 들어감 | 2026-10-27 | `fitbit._local`(UTC 시각 + 시차), 14일 재동기화 |
