"""The OpenAI Codex headless boundary, for roles that should read Smith's output with a different vendor's model
(the report's first reader). Same contract as `smith.headless.run`: one call, structured output pruned and validated
here as untrusted input, `HeadlessError` on any failure.

Boundary (verified against codex-cli 0.164.0 on 2026-10-26; `find_codex` refuses any other version, because
each release has turned new tools on by default — browser_use and computer_use arrived enabled):
- `codex exec -` with the prompt on stdin; `--ephemeral` keeps the session off disk; `--ignore-user-config` and
  `--ignore-rules` skip the user's config.toml (model, MCP servers, trusted projects) and exec-policy rules;
  `project_doc_max_bytes=0` keeps AGENTS.md files out.
- Tools: every tool feature is disabled and `web_search` is off, in an empty temporary working directory under the
  read-only sandbox. Codex still lists a few tools it cannot run (code mode "will fail closed"); an adversarial
  read and web-search prompt attempted nothing and read nothing. The event stream is checked as well: once the turn
  has started, any item other than the answer and its reasoning fails the call closed. Error items before the turn
  are Codex's own start-up notices (an ignored setting, code mode unavailable), not model actions.
- `--output-schema` with a strict copy of the schema (every property required, no extra keys), written to the
  working directory; the answer is read from the `-o` file in UTF-8 (the console code page garbles Korean).
- An allowlisted environment without API keys: the call runs on the ChatGPT-plan login in the Codex home, so it is
  billed to the subscription and follows its data controls (model training is off on this account).
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from smith.headless import HEADLESS_ENV, HeadlessError, Runner, prune, validate

VERSION = "0.164.0"
MODEL = "gpt-6-astra"  # Codex's frontier model on 2026-10-26 (`codex debug models`: "Frontier intelligence").
_MAX_OUTPUT_BYTES = 2_000_000
_ENV_KEEP = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
             "HOMEDRIVE", "HOMEPATH", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "OS",
             "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "CODEX_HOME")
# Features that give the model a tool (codex-cli 0.164.0 `codex features list`).
_TOOL_FEATURES = ("shell_tool", "unified_exec", "unified_exec_tty", "shell_snapshot", "code_mode_host", "multi_agent",
                  "apps", "plugins", "remote_plugin", "goals", "image_generation", "view_image", "browser_use",
                  "browser_use_external", "browser_use_full_cdp_access", "computer_use", "in_app_browser",
                  "in_app_local_automation", "sleep_tool", "skill_search", "skill_mcp_dependency_install",
                  "tool_suggest", "hooks", "workspace_dependencies")
_CONFIG = ("web_search=disabled", "project_doc_max_bytes=0", "history.persistence=none")
_ANSWER_ITEMS = frozenset({"agent_message", "reasoning"})
_NATIVE = Path("node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe")


def find_codex(runner: Runner = subprocess.run) -> Path:
    """Locate the native Codex executable of the verified version, never an npm cmd.exe launcher.

    Raises:
        HeadlessError: "codex-not-found", "unsafe-launcher" or "codex-version" (another version is installed).
    """
    found = shutil.which("codex")
    if found is None:
        raise HeadlessError("codex-not-found")
    path = Path(found)
    if path.suffix.lower() in (".cmd", ".bat", ".ps1", "") and os.name == "nt":
        path = path.parent / _NATIVE
    if not path.is_file() or (os.name == "nt" and path.suffix.lower() != ".exe"):
        raise HeadlessError("unsafe-launcher")
    try:
        proc = runner([str(path), "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                      env=_env(), timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise HeadlessError("launch-failed") from None
    if proc.returncode != 0 or proc.stdout.split()[-1:] != [VERSION]:
        raise HeadlessError("codex-version", proc.stdout.strip()[:40])
    return path


def run(prompt: str, *, schema: dict[str, Any], executable: Path, model: str = MODEL, effort: str = "high",
        timeout_seconds: int, runner: Runner = subprocess.run) -> tuple[Any, dict[str, int] | None]:
    """Run one Codex call and return (pruned and validated structured output, token usage or None).

    The role's instructions travel in `prompt` with its data: Codex has no system-prompt flag that keeps its
    own agent instructions out, and the data is Smith's own text, not a page from the web.

    Raises:
        HeadlessError: the run failed, timed out, tried a tool, or returned output that fails the schema.
    """
    if os.environ.get(HEADLESS_ENV):
        raise HeadlessError("recursive-launch")
    try:
        answer, usage = _exec(prompt, schema, executable, model, effort, timeout_seconds, runner)
    except OSError:  # The working directory or its files (a full disk, a permission): the caller keeps its own text.
        raise HeadlessError("io-failed") from None
    try:
        output = prune(json.loads(answer), schema)
    except ValueError:
        raise HeadlessError("invalid-output") from None
    problems = validate(output, schema, "output")
    if problems:
        raise HeadlessError("schema-violation", ", ".join(problems[:5]))
    return output, usage


def _exec(prompt: str, schema: dict[str, Any], executable: Path, model: str, effort: str, timeout_seconds: int,
          runner: Runner) -> tuple[str, dict[str, int] | None]:
    """(the answer file's text, token usage) of one `codex exec` in a fresh working directory."""
    with tempfile.TemporaryDirectory(prefix="smith-codex-", ignore_cleanup_errors=True) as workdir:
        schema_path, answer_path = Path(workdir, "schema.json"), Path(workdir, "answer.json")
        schema_path.write_text(json.dumps(strict_schema(schema)), encoding="utf-8")
        args = [str(executable), "exec", "-", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                "--ignore-rules", "-C", workdir, "--sandbox", "read-only", "--json", "--output-schema", str(schema_path),
                "-o", str(answer_path), "-m", model, "-c", f"model_reasoning_effort={effort}"]
        for feature in _TOOL_FEATURES:
            args += ["--disable", feature]
        for setting in _CONFIG:
            args += ["-c", setting]
        try:
            proc = runner(args, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env={**_env(), HEADLESS_ENV: "1"}, cwd=workdir, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            raise HeadlessError("timeout") from None
        except OSError:
            raise HeadlessError("launch-failed") from None
        usage = check_events(proc.stdout)
        if proc.returncode != 0:
            raise HeadlessError(f"exit-{proc.returncode}")
        if not answer_path.is_file() or answer_path.stat().st_size > _MAX_OUTPUT_BYTES:
            raise HeadlessError("invalid-output")
        try:
            return answer_path.read_text(encoding="utf-8"), usage
        except UnicodeDecodeError:
            raise HeadlessError("invalid-output") from None


def check_events(stdout: str) -> dict[str, int] | None:
    """Fail closed on the JSONL event stream: once the turn has started, a tool item, an error item or a failed turn
    raises. Returns the completed turn's token usage when reported."""
    usage, started = None, False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue  # Codex prints a few plain log lines on stdout; only events carry items.
        kind = event.get("type") if isinstance(event, dict) else None
        started = started or kind == "turn.started"
        item = event.get("item") if kind in ("item.started", "item.updated", "item.completed") else None
        if started and isinstance(item, dict) and item.get("type") not in _ANSWER_ITEMS:
            raise HeadlessError("tool-attempt", str(item.get("type"))[:40])
        if kind in ("turn.failed", "error"):
            raise HeadlessError("model-error")
        if kind == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = {k: v for k, v in event["usage"].items() if isinstance(v, int)}
    return usage


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema as Codex's strict structured output accepts it: types, properties and items only, every
    property required, no additional properties. Lengths and counts are enforced afterwards by prune/validate."""
    out: dict[str, Any] = {"type": schema["type"]}
    if "enum" in schema:
        out["enum"] = list(schema["enum"])
    if schema["type"] == "object":
        out["properties"] = {key: strict_schema(sub) for key, sub in schema["properties"].items()}
        out["required"] = list(schema["properties"])
        out["additionalProperties"] = False
    elif schema["type"] == "array":
        out["items"] = strict_schema(schema["items"])
    return out


def _env() -> dict[str, str]:
    return {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
