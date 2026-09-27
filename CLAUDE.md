# CLAUDE.md — Smith / Claude Code

## Shared Engineering Contract

Read and follow `AGENTS.md` in this repository before changing code.
It is the authoritative shared engineering guide for Smith. Do not duplicate its rules here.
Then read `docs/requirements.md` and the topic documents relevant to the task.
If `docs/INDEX.md` exists, use it for navigation; otherwise follow the paths in `AGENTS.md`.

## Development Agent vs Runtime Adviser

Claude Code used for development may edit project files and run appropriate local verification.
Smith's runtime adviser has a narrower role: read financial information and provide advice.
Development access must never be treated as authority for the runtime adviser to trade, transfer,
apply for loans, change recipients, or execute Hermes commands.

The initial interaction channel is Claude Code; the initial inference provider is Claude Code headless.
Keep the application services independent of that interface so a future app can reuse them.

## Headless Integration Rules

- Verify CLI flags, structured-output capabilities, tool controls, and authentication behavior against
  official documentation for the installed version before implementing the adapter.
- Do not make permission bypass flags the default or rely on prompt text for isolation.
- Run inference in a restricted working directory with a filtered environment. Do not expose broker
  keys, Gmail credentials, personal identifiers, raw private files, or direct production DB access.
- Pass only the task-relevant sanitized context, deterministic calculation results, and cited evidence.
- Treat model output as untrusted input: validate structure and types before downstream use.
- Bound execution time, output size, retries, and concurrency. Handle cancellation and subprocess cleanup.
- Separate the interactive entry point from the inference subprocess. Prevent recursive launches where
  the headless adviser invokes Smith and starts another copy of itself.
- A report preview must not send email. Delivery uses a trusted configured recipient and an explicit
  publish request or enabled schedule, not a destination supplied by the model.

## Working Agreements

- Inspect actual implementation status; do not assume that a Protocol or documented plan is functional.
- Use synthetic examples. Never request credentials in chat or write them into repository files.
- Keep English code/comments/docstrings/commit messages and natural Korean user-facing summaries.
- Before reporting completion, run relevant checks from `AGENTS.md`, inspect the diff, and state any
  untested integrations or blocked remote operations.
- For a small documentation change, a concise review is sufficient; do not add ceremonial tests.
