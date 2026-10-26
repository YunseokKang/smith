import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smith import codex, reader
from smith.headless import HEADLESS_ENV, HeadlessError

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["notes"],
          "properties": {"notes": {"type": "array", "maxItems": 2, "items": {"type": "string", "maxLength": 5}}}}


def events(*items, failed=False):
    lines = [{"type": "thread.started"},
             {"type": "item.completed", "item": {"type": "error", "message": "ignoring an unrecognized setting"}},
             {"type": "turn.started"}]
    lines += [{"type": "item.completed", "item": {"type": kind}} for kind in items]
    lines.append({"type": "turn.failed"} if failed else {"type": "turn.completed", "usage": {"input_tokens": 9}})
    return "\n".join(json.dumps(line) for line in lines)


def codex_runner(answer, stdout=None, calls=None):
    def run(args, **kwargs):
        if calls is not None:
            calls.append((args, kwargs))
        Path(args[args.index("-o") + 1]).write_text(json.dumps(answer), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, events("reasoning", "agent_message") if stdout is None else stdout, "")
    return run


class CodexTests(unittest.TestCase):
    def test_run_disables_tools_filters_the_environment_and_validates(self):
        calls = []
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "ANTHROPIC_API_KEY": "x"}):
            output, usage = codex.run("읽어 주세요", schema=SCHEMA, executable=Path("codex.exe"), timeout_seconds=5,
                                      runner=codex_runner({"notes": ["가나다라마바사", "b", "c"]}, calls=calls))
        self.assertEqual(output, {"notes": ["가나다라마", "b"]})              # Pruned to the schema's limits.
        self.assertEqual(usage, {"input_tokens": 9})
        args, kwargs = calls[0]
        self.assertEqual(kwargs["input"], "읽어 주세요")                       # Data on stdin, not in arguments.
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "web_search=disabled", "read-only"):
            self.assertIn(flag, args)
        disabled = {args[i + 1] for i, a in enumerate(args) if a == "--disable"}
        self.assertLessEqual({"shell_tool", "unified_exec", "browser_use", "computer_use", "multi_agent"}, disabled)
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn("ANTHROPIC_API_KEY", kwargs["env"])
        self.assertEqual(kwargs["env"][HEADLESS_ENV], "1")
        self.assertFalse(Path(kwargs["cwd"]).exists())                        # The working directory is cleaned up.

    def test_failures_are_closed(self):
        cases = {"tool": (events("command_execution"), "tool-attempt"),
                 "error after the turn started": (events("error"), "tool-attempt"),
                 "failed turn": (events(failed=True), "model-error")}
        for name, (stdout, code) in cases.items():
            with self.subTest(name), self.assertRaises(HeadlessError) as raised:
                codex.run("p", schema=SCHEMA, executable=Path("codex.exe"), timeout_seconds=5,
                          runner=codex_runner({"notes": []}, stdout=stdout))
            self.assertEqual(raised.exception.code, code)
        with self.assertRaises(HeadlessError) as raised:
            codex.run("p", schema=SCHEMA, executable=Path("codex.exe"), timeout_seconds=5,
                      runner=codex_runner({"other": 1}))
        self.assertEqual(raised.exception.code, "schema-violation")
        with mock.patch.object(codex.tempfile, "TemporaryDirectory", side_effect=OSError("disk full")),                 self.assertRaises(HeadlessError) as raised:                  # Never an OSError for the caller.
            codex.run("p", schema=SCHEMA, executable=Path("codex.exe"), timeout_seconds=5, runner=codex_runner({}))
        self.assertEqual(raised.exception.code, "io-failed")
        with mock.patch.dict(os.environ, {HEADLESS_ENV: "1"}), self.assertRaises(HeadlessError) as raised:
            codex.run("p", schema=SCHEMA, executable=Path("codex.exe"), timeout_seconds=5, runner=codex_runner({}))
        self.assertEqual(raised.exception.code, "recursive-launch")

    def test_strict_schema_requires_every_property_and_drops_limits(self):
        strict = codex.strict_schema(reader.SCHEMA)
        note = strict["properties"]["notes"]["items"]
        self.assertEqual(note["required"], list(note["properties"]))
        self.assertFalse(note["additionalProperties"])
        self.assertEqual(note["properties"]["problem"]["enum"], list(reader.PROBLEMS))
        self.assertNotIn("maxLength", json.dumps(strict))

    def test_only_the_verified_version_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp, "codex.exe")
            exe.write_bytes(b"")
            for stdout, ok in ((f"codex-cli {codex.VERSION}\n", True), ("codex-cli 0.165.0\n", False)):
                version = lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout, "")  # noqa: E731
                with self.subTest(stdout), mock.patch.object(codex.shutil, "which", return_value=str(exe)):
                    if ok:
                        self.assertEqual(codex.find_codex(version), exe)
                    else:
                        with self.assertRaises(HeadlessError) as raised:
                            codex.find_codex(version)
                        self.assertEqual(raised.exception.code, "codex-version")
        with mock.patch.object(codex.shutil, "which", return_value=None):
            self.assertEqual(reader.installed(), (None, "codex-not-found"))


class ReaderTests(unittest.TestCase):
    OUTPUT = {"situation": {"text": "듀레이션 리스크가 커졌습니다.", "refs": ["R1"]}, "direction": {"text": "", "refs": []},
              "portfolio": {"text": "쏠림이 있습니다.", "refs": [], "principles": []},
              "holdings": [{"symbol": "VOO", "stance": "hold", "reason": "넓게 나눠 담은 상품입니다.", "alternative": "",
                            "reconsider": "비중이 커지면 다시 봅니다.", "refs": []}],
              "watch": [{"item": "전세 만기", "why": "자금이 묶일 수 있습니다.", "refs": [], "memo": "출처 확인"}]}

    def test_blocks_are_prose_of_passing_blocks_only(self):
        shown = reader.blocks(self.OUTPUT, {"portfolio": "memo"})
        self.assertEqual([b["path"] for b in shown], ["situation", "holdings[0]"])
        self.assertEqual(shown[1]["text"], "넓게 나눠 담은 상품입니다.\n비중이 커지면 다시 봅니다.")

    def test_only_notes_on_real_passages_are_kept(self):
        shown = reader.blocks(self.OUTPUT)
        note = {"path": "situation", "quote": "듀레이션  리스크", "problem": "jargon", "why": "w", "suggestion": "s"}
        raw = [note, dict(note),                                               # Duplicate.
               dict(note, path="watch[0]"),                                     # Not shown (memo).
               dict(note, quote="금리가 오릅니다"),                               # Not in the block.
               dict(note, problem="grammar"),                                   # Unknown kind.
               dict(note, path="holdings[0]", quote="비중이 커지면", problem="vague")]
        kept = reader.clean_notes(raw, shown)
        self.assertEqual([(n["path"], n["quote"]) for n in kept], [("situation", "듀레이션  리스크"),
                                                                   ("holdings[0]", "비중이 커지면")])


if __name__ == "__main__":
    unittest.main()
