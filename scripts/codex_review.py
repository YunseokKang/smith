"""Second-opinion code review by OpenAI Codex (a different vendor from the Claude development agent).

Usage (PowerShell, from the repository root):
    .\\.venv\\Scripts\\python.exe scripts\\codex_review.py                 # uncommitted changes
    .\\.venv\\Scripts\\python.exe scripts\\codex_review.py --commit HEAD   # one commit
    .\\.venv\\Scripts\\python.exe scripts\\codex_review.py --base main     # a branch against its base

Codex runs in the read-only sandbox with the reviewer brief below (its shell reads the diff and may run focused tests;
web search is off) and returns findings as JSON;
this prints them most severe first and exits 1 when a P0 or P1 finding remains. The brief tells Codex never to read
data/, local configuration or credentials: the sandbox blocks writes, not reads, so this is an instruction, not a
control. Development tool only; the runtime adviser never calls it.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from smith import codex  # noqa: E402 - the verified executable and the strict-schema helper.

EFFORT = "xhigh"
TIMEOUT_SECONDS = 1800
SEVERITIES = ("P0", "P1", "P2", "P3")
SCHEMA = {
    "type": "object", "required": ["findings", "verdict", "checked"],
    "properties": {
        "findings": {"type": "array", "items": {
            "type": "object", "required": ["severity", "file", "line", "title", "failure_scenario", "fix"],
            "properties": {"severity": {"type": "string", "enum": list(SEVERITIES)}, "file": {"type": "string"},
                           "line": {"type": "string"}, "title": {"type": "string"},
                           "failure_scenario": {"type": "string"}, "fix": {"type": "string"}}}},
        "verdict": {"type": "string"},
        "checked": {"type": "array", "items": {"type": "string"}},
    },
}

BRIEF = """You are a principal engineer giving an independent second review of a change to Smith, before it is
committed. The author is another AI coding agent; you are a different model on purpose, so look for what that agent
would miss rather than restating what it did.

Smith is a read-only personal wealth adviser for one Korean household (Python 3.12, standard library first, Windows
and PowerShell). It reads financial records, computes figures in code, has headless LLM calls interpret them, and
mails reports to one configured recipient. AGENTS.md at the repository root is the engineering contract: read it
first, then the documents it points to that the change touches.

Review target: {target}

Report only defects a careful senior reviewer would block or insist on fixing, each with a concrete failure scenario
(inputs or state, then the wrong result, crash or leak). Priorities:
- P0: money moved or a financial action exposed; secrets, account numbers or household data leaking (into a
  prompt, a log, a repository file, an email to anyone but the configured recipient); a report preview that sends.
- P1: wrong money arithmetic (binary floats, rounding, currency mix-ups, unknown treated as zero); an LLM output
  used without validation; a headless call with tools, an unfiltered environment or no time bound; a model able to
  start another model or Smith; silent failure presented as success; lost or duplicated deliveries; broken
  concurrency or cleanup; a behavior change with no test where the risk is real.
- P2: missed edge cases, misleading docs or contracts updated out of step with the code, Windows encoding traps
  (implicit cp949), test-suite cost (a unit test needing network, subprocesses, sleeps or more than ~100 ms).
- P3: worth a note, not a fix now.
No style nits, no praise, no restating the diff. Prefer zero findings to weak ones; say so in the verdict.

You may read code and run focused checks, for example
`.\\.venv\\Scripts\\python.exe -B -m unittest tests.test_narrative -v`. Never modify files. Never open data/,
config/*.local.toml, .env files, reports/, anything under the user's home directory, or credential stores: they hold
the household's real records and secrets, and nothing there is needed to review code.

Answer with JSON: findings (severity, file, line, title, failure_scenario, fix), verdict (two or three sentences),
checked (what you read or ran to verify).
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--commit", help="review the changes introduced by this commit")
    target.add_argument("--base", help="review the current branch against this base branch")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")  # A Korean console (cp949) cannot print every character (TS-008).
    if args.commit:
        what = f"the changes introduced by commit {args.commit} (`git show {args.commit}`)."
    elif args.base:
        what = f"the current branch against {args.base} (`git diff {args.base}...HEAD`)."
    else:
        what = ("the uncommitted changes: `git diff HEAD` plus untracked files under src/, tests/, scripts/ and docs/ "
                "(`git status --short`).")
    repo = Path(__file__).resolve().parents[1]
    executable = codex.find_codex()
    with tempfile.TemporaryDirectory(prefix="smith-review-") as tmp:
        schema_path, answer_path = Path(tmp, "schema.json"), Path(tmp, "answer.json")
        schema_path.write_text(json.dumps(codex.strict_schema(SCHEMA)), encoding="utf-8")
        # Plain `exec`, not `exec review`: review mode ignores --output-schema (checked on 0.164.0). The user's
        # config is kept: without its Windows sandbox setting every read command was "blocked by policy".
        command = [str(executable), "exec", "-", "--ephemeral",
                   "--output-schema", str(schema_path), "-o", str(answer_path), "-m", codex.MODEL,
                   "-c", f"model_reasoning_effort={EFFORT}", "-c", "sandbox_mode=read-only",
                   "-c", "web_search=disabled"]
        proc = subprocess.run(command, input=BRIEF.format(target=what), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", cwd=repo, timeout=TIMEOUT_SECONDS)
        answer = answer_path.read_text(encoding="utf-8") if answer_path.is_file() else ""
    try:
        review = json.loads(answer)
    except ValueError:
        print(f"codex review failed (exit {proc.returncode}): {answer[:2000]}", file=sys.stderr)
        return 2
    findings = sorted(review["findings"], key=lambda f: SEVERITIES.index(f["severity"]))
    for f in findings:
        print(f"[{f['severity']}] {f['file']}:{f['line']} {f['title']}\n  scenario: {f['failure_scenario']}\n"
              f"  fix: {f['fix']}\n")
    print(f"verdict: {review['verdict']}\nchecked: {'; '.join(review['checked'])}")
    return 1 if any(f["severity"] in ("P0", "P1") for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
