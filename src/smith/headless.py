"""The Claude Code headless boundary shared by every model call (advice, research, narrative).

Boundary (verified against Claude Code 2.1.195 `claude --help` on 2026-10-04 and 2026-10-05):
- `-p` with the prompt on stdin, so data never appears in arguments or process lists.
- Tools: `--tools ""` disables every tool for calls that see household data. A research call that must
  read the web gets exactly `WebSearch,WebFetch` (`--tools` limits the available set, `--allowedTools`
  pre-approves those two, `--permission-mode dontAsk` refuses anything else without prompting). Such a
  call never receives household data, so a hostile web page has nothing to exfiltrate.
- `--safe-mode` skips user CLAUDE.md, hooks, plugins and MCP; `--no-session-persistence` keeps the
  conversation off disk; `--system-prompt` replaces the default prompt.
- `--json-schema` asks for structured output, which is pruned and validated here as untrusted input.
- An empty temporary working directory, an allowlisted environment, a timeout and a spend cap.
- The native `claude.exe` is launched directly; a `.cmd` launcher would route arguments through cmd.exe.
"""
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

HEADLESS_ENV = "SMITH_HEADLESS"
WEB_TOOLS = ("WebSearch", "WebFetch")
_MAX_OUTPUT_BYTES = 2_000_000
_MAX_TEXT = 4000
_ENV_KEEP = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
             "HOMEDRIVE", "HOMEPATH", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "OS",
             "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR")

Runner = Callable[..., subprocess.CompletedProcess]


class HeadlessError(Exception):
    """A model call could not produce validated output. Carries a short code and, for validation
    failures, a detail made only of field paths or ref names (never amounts or text)."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if detail is None else f"{code}: {detail}")
        self.code, self.detail = code, detail


def find_claude() -> Path:
    """Locate the native Claude Code executable, never a cmd.exe launcher."""
    found = shutil.which("claude")
    if found is None:
        raise HeadlessError("claude-not-found")
    path = Path(found)
    if path.suffix.lower() == ".exe":
        return path
    native = path.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    if path.suffix.lower() in (".cmd", ".bat") and native.is_file():
        return native
    if os.name != "nt" and path.suffix == "":
        return path
    raise HeadlessError("unsafe-launcher")


def run(prompt: str, *, system_prompt: str, schema: dict[str, Any], executable: Path, model: str | None,
        budget_usd: str, timeout_seconds: int, tools: Sequence[str] = (),
        runner: Runner = subprocess.run) -> tuple[Any, str | None]:
    """Run one headless call and return (pruned and validated structured output, cost in USD or None).

    Raises:
        HeadlessError: the run failed, timed out, or returned output that fails the schema.
    """
    if os.environ.get(HEADLESS_ENV):
        raise HeadlessError("recursive-launch")  # Defense in depth: no call can start another Smith.
    if set(tools) - set(WEB_TOOLS):
        raise HeadlessError("tool-not-allowed")
    validate_budget(budget_usd)
    args = [str(executable), "-p"]
    if tools:
        listed = ",".join(tools)
        args += ["--tools", listed, "--allowedTools", listed, "--permission-mode", "dontAsk"]
    else:
        args += ["--tools", ""]
    # `--tools` governs built-in tools only; MCP tools are denied explicitly as defense in depth.
    args += ["--disallowedTools", "mcp__*", "--safe-mode", "--no-session-persistence", "--output-format", "json",
             "--json-schema", json.dumps(cli_schema(schema)), "--system-prompt", system_prompt,
             "--max-budget-usd", budget_usd]
    if model:
        args += ["--model", model]
    env = {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
    env[HEADLESS_ENV] = "1"
    with tempfile.TemporaryDirectory(prefix="smith-headless-") as workdir:
        try:
            proc = runner(args, input=prompt, capture_output=True, text=True, encoding="utf-8", env=env,
                          cwd=workdir, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            raise HeadlessError("timeout") from None
        except OSError:
            raise HeadlessError("launch-failed") from None
    if proc.returncode != 0:
        raise HeadlessError(f"exit-{proc.returncode}", _error_subtype(proc.stdout))
    if len(proc.stdout.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        raise HeadlessError("output-too-large")
    try:
        envelope = json.loads(proc.stdout)
    except ValueError:
        raise HeadlessError("invalid-output") from None
    if not isinstance(envelope, dict) or envelope.get("is_error") or envelope.get("subtype") != "success":
        raise HeadlessError("model-error")
    output = prune(envelope.get("structured_output"), schema)
    problems = validate(output, schema, "output")
    if problems:
        raise HeadlessError("schema-violation", ", ".join(problems[:5]))
    return output, validated_cost(envelope.get("total_cost_usd"))


def cli_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema with only types and required fields. With length, count or additionalProperties
    constraints the CLI's structured-output retries failed on real payloads
    (error_max_structured_output_retries); those rules are enforced locally by `prune` and `validate`."""
    loose = {key: value for key, value in schema.items()
             if key not in ("maxLength", "maxItems", "minItems", "additionalProperties")}
    if "properties" in loose:
        loose["properties"] = {key: cli_schema(value) for key, value in loose["properties"].items()}
    if "items" in loose:
        loose["items"] = cli_schema(loose["items"])
    return loose


def validate_budget(value: str) -> None:
    try:
        budget = Decimal(value)
    except (InvalidOperation, ValueError):
        raise HeadlessError("invalid-budget") from None
    if not budget.is_finite() or budget <= 0:
        raise HeadlessError("invalid-budget")


def validated_cost(value: Any) -> str | None:
    """A missing cost is recorded as unknown; a malformed one is an error."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise HeadlessError("invalid-cost")
    try:
        cost = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HeadlessError("invalid-cost") from None
    if not cost.is_finite() or cost < 0:
        raise HeadlessError("invalid-cost")
    return str(cost)


def _error_subtype(stdout: str) -> str | None:
    """The CLI's result subtype (for example error_max_budget_usd), which names the failure safely."""
    try:
        envelope = json.loads(stdout)
    except ValueError:
        return None
    subtype = envelope.get("subtype") if isinstance(envelope, dict) else None
    return subtype if isinstance(subtype, str) and subtype.replace("_", "").isalnum() else None


def prune(value: Any, schema: dict[str, Any]) -> Any:
    """Drop object keys the schema does not define; only known fields are ever used or stored."""
    if schema["type"] == "object" and isinstance(value, dict):
        return {key: prune(item, schema["properties"][key]) for key, item in value.items() if key in schema["properties"]}
    if schema["type"] == "array" and isinstance(value, list):
        return [prune(item, schema["items"]) for item in value]
    return value


def validate(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    """Check the subset of JSON Schema the Smith schemas use. Model output is untrusted input."""
    kind = schema["type"]
    if kind == "string":
        ok = isinstance(value, str) and len(value) <= schema.get("maxLength", _MAX_TEXT)
        return [] if ok else [path]
    if kind == "array":
        if not isinstance(value, list) or not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            return [path]
        return [p for i, item in enumerate(value) for p in validate(item, schema["items"], f"{path}[{i}]")]
    if not isinstance(value, dict) or set(value) - set(schema["properties"]) or set(schema["required"]) - set(value):
        return [path]
    return [p for key, sub in schema["properties"].items() if key in value
            for p in validate(value[key], sub, f"{path}.{key}")]
