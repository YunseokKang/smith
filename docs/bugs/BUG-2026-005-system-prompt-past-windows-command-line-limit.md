# BUG-2026-005 해석 지시문이 Windows 명령줄 한도를 넘어 부동산 레터가 AI 해석 없이 발송됨

- 발견: 2026-10-28 아침(KST). 사용자가 부동산 레터 끝의 "▲ 확인 필요: 이번에는 AI 해석을 만들지 못해 계산 결과만으로
  작성했습니다" 문구를 보고 원인을 물었다.
- 영향: 2026-10-28 06:04 부동산 레터(run 9085e9e5)가 계산 결과만으로 발송됐다. 조사(35건)는 정상이었고 해석 단계만
  빠졌다. 고치기 전까지는 모든 주간 보고의 해석과 종목 추천 검토팀 편집자(`team.EDITOR_PROMPT`)가 같은 이유로 실패할
  상태였다. 데일리·저녁·건강 메일과 보고 답장 응답은 지시문이 짧아 영향이 없었다.
- 기록: `data/report-runs.log`의 `Narrative (property) failure: launch-failed`. 조사 직후 1초 만에 실패했다.

## 원인

`headless.run`은 시스템 프롬프트를 `--system-prompt` 인자로 넘겼다. Windows `CreateProcess`는 명령줄 전체를 32,767자까지만
받는다. 2026-10-27 커밋 e223112가 모든 작성용 프롬프트에 `korean.RULES`(약 1,000자)를 붙이면서 `narrative.SYSTEM_PROMPT`가
30,071자가 됐고, 스키마(2,219자)와 나머지 인자를 합친 명령줄은 32,996자가 됐다. `subprocess.run`이
`FileNotFoundError [WinError 206] 파일 이름이나 확장명이 너무 깁니다`를 냈고, `headless.run`은 이것을 `launch-failed`로
바꿨다. 같은 날 시장 보고(06:17)는 커밋보다 먼저 실행돼 영향이 없었다.

## 수정

- 시스템 프롬프트를 임시 작업 폴더의 `system-prompt.txt`(UTF-8)에 쓰고 `--system-prompt-file`로 넘긴다. 이 옵션은
  Claude Code 2.1.302에서 인라인 방식과 같은 결과를 내는 것을 확인했다(합성 지시문 비교, 2026-10-28).
- 실행 전에 명령줄 길이를 재서 32,000자를 넘으면 `command-too-long`으로 거부한다. 같은 실패가 다시 생기면 원인이 이름으로
  남는다.

## 검증·예방

- 검증: 실제 `narrative.SYSTEM_PROMPT`(한국어 규칙 포함)로 실제 호출을 한 번 했고, 모델이 파일 속 규칙 9의 한국어 예시를
  그대로 답했다. 테스트(`tests/test_narrative.py` `test_the_longest_system_prompts_travel_in_a_file_…`)는 가장 긴 두 지시문이
  파일로 전달되는지와 긴 명령줄이 거부되는지 확인한다. 전체 테스트 400개가 통과했다.
- 기존 테스트가 놓친 이유: 테스트는 가짜 runner로 인자 목록만 읽고 실제 프로세스를 띄우지 않으므로 OS 한도에 닿지 않았다.
- 예방: 긴 입력은 stdin이나 파일로 넘긴다. 명령줄에는 짧은 옵션만 둔다. Codex 호출은 이미 지시문을 stdin으로, 스키마를
  파일로 넘긴다.
