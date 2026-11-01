"""Evernote notes read through the Aside browser (daily design §13.1, user decision 2026-11-01).

The client writes in Evernote every day on the free (Basic) plan. Evernote issues no new API keys and its official MCP
server excludes Basic accounts (dev.evernote.com/mcp/faq, checked 2026-11-01), so Smith reads the web client the
client is signed in to inside the Aside browser. `aside repl` runs a fixed script here in code (never Aside's own
agent): it opens its own tab, lists the allowed notebooks and spaces, opens each note by its GUID and prints the
title, body and "수정됨" line, then closes the tab. Evernote's terms allow user-authorized automated agents but not
other interfaces or server load: only the web UI, one note at a time, at most once a day, and nothing is ever typed.

Only `[evernote] notebooks` and `spaces` are read (an allowlist: a new space stays unread until the client adds it).
Notes are stored in `data/evernote/notes.json` and enter the Keep paths as `keep.Note`s: searched for questions,
gift and partner notes, and (notebooks only) offered for the sentence of the day. A source that fails or lists short
keeps its earlier copy, so a bad run never empties the store.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from smith import keep

logger = logging.getLogger(__name__)

NOTES_FILE = "notes.json"
SYNC_FILE = "sync.json"
LOCK_FILE = "sync.lock"
REDACTED = "[가림: 민감 정보 줄]"
WEB = "https://www.evernote.com/client/web"
MARK = "SMITH-EN "
BATCH = 60                      # Notes per `aside repl` call: about 75 s at ~1.2 s a note.
CALL_BUDGET_MS = 90_000         # The script stops itself before Aside's 120 s limit and reports where.
CALL_TIMEOUT = 118              # Seconds before Smith kills the CLI.
LIST_CALLS = 6                  # Calls one source's list may take (a 255-note notebook needs about two).
MAX_NOTES = 3000
MAX_TEXT = 200_000
RETRY_AFTER = timedelta(hours=1)
LABEL = "evernote"
SPACE_LABEL, NOTEBOOK_LABEL = "evernote-space", "evernote-notebook"
_UI_LINES = {"헤더 링크 복사", "불러오는 중…", "불러오는 중...", "Copy header link"}
_SECRET_LINE = re.compile(r"(?i)pass\s*word|passwd|비밀\s*번호|비\s*번|\bpw\b|\bpin\b|계좌\s*번호|카드\s*번호|주민\s*등록|\bcvc\b|\botp\b"
                          r"|인증\s*번호|보안\s*카드|account\s*(?:no|number)|api[\s_-]*key|access[\s_-]*token"
                          r"|secret[\s_-]*key|client[\s_-]*secret|private[\s_-]*key|bearer\s|api\s*키|시크릿|토큰\s*값")
# A run of ten or more digits (up to three spaces, hyphens, dots or dashes between them) is an account, card or ID
# number even with no marker beside it, and a long mixed letter-digit string is a key or token; both are masked in
# everything a model can get (reviews 2026-11-01). A bare "token" is not a marker: the client's AI study notes use it.
_LONG_NUMBER = re.compile(r"\d(?:[\s.\-–—]{0,3}\d){9,}")
_LONG_KEY = re.compile(r"(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{24,}")
_FULL = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")
_DOTS = re.compile(r"(\d{2})\.\s*(\d{1,2})\.\s*(\d{1,2})\.")
_MONTH_DAY = re.compile(r"(\d{1,2})월\s*(\d{1,2})일")

Runner = Callable[[list[str], float], tuple[int, str]]


class EvernoteError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Listed:
    guid: str
    source: str
    kind: str          # "notebook" | "space"
    title: str | None  # From the list; the read checks the editor shows this title.


def directory(db: Path) -> Path:
    return Path(db).parent / "evernote"


# --- Aside adapter ---------------------------------------------------------------------------------------------------

def find_aside() -> Path:
    """The Aside CLI executable (a native .exe, never a .cmd wrapper: TS-010). Raises EvernoteError."""
    local = os.environ.get("LOCALAPPDATA")
    candidates = [Path(local) / "Aside" / "CLI" / "current" / "aside.exe"] if local else []
    found = shutil.which("aside")
    candidates += [Path(found)] if found else []
    for path in candidates:
        if path.is_file() and path.suffix.lower() == ".exe":
            return path
    raise EvernoteError("aside-not-found")


def run_aside(args: list[str], timeout: float) -> tuple[int, str]:
    """Run the Aside CLI; returns (exit code, stdout). A timeout returns -1 with what was printed so far."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=flags, check=False)
        return done.returncode, done.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as expired:
        out = expired.stdout or b""
        return -1, out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)


def _call(script: str, args: dict[str, Any], runner: Runner, executable: Path) -> list[dict[str, Any]]:
    """Run one script; returns its SMITH-EN records. Raises EvernoteError when nothing usable came back."""
    code = script.replace("__ARGS__", json.dumps(args, ensure_ascii=False))
    status, out = runner([str(executable), "repl", code], CALL_TIMEOUT)
    records = []
    for line in out.splitlines():
        if line.startswith(MARK):
            try:
                record = json.loads(line[len(MARK):])
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
    errors = [r for r in records if r.get("kind") == "error"]
    if errors:
        raise EvernoteError(str(errors[0].get("code", "script-error"))[:40])
    if not records:
        raise EvernoteError("aside-timeout" if status == -1 else "aside-failed")
    return records


# --- the scripts (run inside Aside; read-only: they navigate and read, never type or click a note's controls) ----------

_PRELUDE = r"""
const ARGS = __ARGS__;
const T0 = Date.now();
const emit = (kind, body) => console.log('SMITH-EN ' + JSON.stringify(Object.assign({ kind }, body || {})));
const tab = await openTab(ARGS.url);
async function appReady() {
  for (let i = 0; i < 60; i++) {
    if (await tab.evaluate(() => !!document.getElementById('qa-NAV_ALL_NOTEBOOKS'))) return true;
    await sleep(500);
  }
  return false;
}
async function go(hash) {
  await tab.evaluate(h => { location.hash = h; }, hash);
  await sleep(1200);
}
"""

LIST_SCRIPT = _PRELUDE + r"""
const ROW = { space: '[id^="qa-NOTE_ROW_"]', notebook: 'button[id$="_qa-NOTES_SIDEBAR_NOTE"]' };
async function scrollTo(kind, top) {
  // Returns the list's scroll position after the move (null when there is no scrollable list); `top` null reads it.
  return tab.evaluate(([sel, to]) => {
    const first = document.querySelector(sel);
    let box = first ? first.parentElement : null;
    while (box && !(box.scrollHeight > box.clientHeight + 4 && /(auto|scroll)/.test(getComputedStyle(box).overflowY))) box = box.parentElement;
    if (!box) return null;
    if (to !== null) box.scrollTop = to;
    return box.scrollTop;
  }, [ROW[kind], top]);
}
async function collect(kind, expected, start, have) {
  // Stop after ~1 s without progress once the count the page shows is reached; short of it, the list may still be
  // fetching its next page from the server, so keep nudging for up to ~30 s without progress (first sync 2026-11-01:
  // 229 of 255 with the short wait while another run loaded Aside). A call stops itself at its budget and reports
  // the scroll position, and the next call carries on from there (`have`: rows the earlier calls already saw).
  const seen = new Map();
  let idle = 0;
  if (start) { await scrollTo(kind, start); await sleep(800); }
  const patience = () => (expected !== null && have + seen.size < expected) ? 75 : 3;
  for (let round = 0; round < 600 && idle < patience(); round++) {
    if (Date.now() - T0 > ARGS.budget_ms) return { rows: [...seen.values()], resume: await scrollTo(kind, null) };
    const rows = await tab.evaluate((k) => {
      if (k === 'space') {
        return [...document.querySelectorAll('[id^="qa-NOTE_ROW_"]')].map(r => {
          const guid = r.id.replace(/^qa-NOTE_ROW_\d+_/, '');
          const t = document.getElementById('qa-SPACE_VIEW_NOTE_TITLE_' + guid);
          return { guid, title: t ? t.innerText.trim() : null };
        });
      }
      return [...document.querySelectorAll('button[id$="_qa-NOTES_SIDEBAR_NOTE"]')].map(b => {
        const guid = b.id.replace(/_qa-NOTES_SIDEBAR_NOTE$/, '');
        const t = document.getElementById(guid + '_qa-NOTES_SIDEBAR_NOTE_TITLE');
        return { guid, title: t ? t.innerText.trim() : null };
      });
    }, kind);
    let added = 0;
    for (const r of rows) if (!seen.has(r.guid)) { seen.set(r.guid, r); added++; }
    const moved = await tab.evaluate((k) => {
      const first = document.querySelector(k === 'space' ? '[id^="qa-NOTE_ROW_"]' : 'button[id$="_qa-NOTES_SIDEBAR_NOTE"]');
      let box = first ? first.parentElement : null;
      while (box && !(box.scrollHeight > box.clientHeight + 4 && /(auto|scroll)/.test(getComputedStyle(box).overflowY))) box = box.parentElement;
      if (!box) return false;
      const before = box.scrollTop;
      const atEnd = before + box.clientHeight >= box.scrollHeight - 2;
      // Small steps: the virtual list draws only rows near the view, and a long jump outran its drawing (rows in the
      // middle were never on the page; 206-239 of 255). At the end, step back a little so a list that loads on
      // reaching the bottom is asked again on the next step down.
      box.scrollTop = atEnd ? Math.max(0, before - 300) : before + Math.max(80, Math.floor(box.clientHeight * 0.4));
      return box.scrollTop !== before;
    }, kind);
    idle = added === 0 ? idle + 1 : 0;
    if (!moved && idle >= 3) break;   // Nothing scrollable and nothing new: the whole list is on the page.
    // Wait until the list has drawn the rows of the new position (the first row it shows changes), at most ~1 s.
    const head = rows.length ? rows[0].guid : null;
    for (let w = 0; w < 7; w++) {
      await sleep(150);
      const now = await tab.evaluate((k) => {
        const first = document.querySelector(k === 'space' ? '[id^="qa-NOTE_ROW_"]' : 'button[id$="_qa-NOTES_SIDEBAR_NOTE"]');
        return first ? first.id : null;
      }, kind);
      if (now && head && !now.includes(head)) break;
    }
  }
  return { rows: [...seen.values()], resume: null };
}
function report(name, kind, guid, expected, found) {
  for (const r of found.rows) emit('listed', { source: name, source_kind: kind, guid: r.guid, title: r.title });
  emit('source', { name, source_kind: kind, guid, expected, found: found.rows.length, resume: found.resume });
}
async function notebook(name) {
  if (ARGS.guid) {
    await go('#/notebook/' + ARGS.guid);
    report(name, 'notebook', ARGS.guid, ARGS.expected, await collect('notebook', ARGS.expected, ARGS.start, ARGS.have));
    return;
  }
  await go('#/notebooks');
  let guid = null;
  for (let i = 0; i < 10 && !guid; i++) {
    guid = await tab.evaluate((n) => {
      for (const r of document.querySelectorAll('[id^="qa-NOTEBOOK_ROW_"]')) {
        const first = (r.innerText || '').split('\n').map(s => s.trim()).find(Boolean);
        if (first === n) return r.id.replace(/^qa-NOTEBOOK_ROW_\d+_/, '');
      }
      return null;
    }, name);
    if (!guid) await sleep(500);
  }
  if (!guid) { emit('source', { name, source_kind: 'notebook', error: 'not-found' }); return; }
  await go('#/notebook/' + guid);
  const expected = await tab.evaluate(() => {
    const box = document.querySelector('[aria-label="노트 목록"] header') || document.querySelector('[aria-label="노트 목록"]');
    const nums = ((box && box.innerText) || '').split('\n')[0].match(/\d[\d,]*/g);
    return nums ? Number(nums[nums.length - 1].replace(/,/g, '')) : null;
  });
  report(name, 'notebook', guid, expected, await collect('notebook', expected, 0, 0));
}
async function closeInvite() {
  await tab.evaluate(() => {
    const dialog = document.querySelector('[role="dialog"]');
    if (!dialog) return;
    const close = [...dialog.querySelectorAll('button')].find(b => ((b.getAttribute('aria-label') || b.innerText || '').trim() === '닫기'));
    if (close) close.click();
  });
  await sleep(400);
}
async function space(name) {
  if (ARGS.guid) {
    await go('#/space/' + ARGS.guid);
    await closeInvite();
    report(name, 'space', ARGS.guid, ARGS.expected, await collect('space', ARGS.expected, ARGS.start, ARGS.have));
    return;
  }
  const find = () => tab.evaluate((n) => {
    const item = [...document.querySelectorAll('[role="treeitem"]')].find(e => (e.innerText || '').trim() === n);
    if (item) { item.click(); return true; }
    return false;
  }, name);
  if (!(await find())) {
    await tab.evaluate(() => {
      const toggle = [...document.querySelectorAll('button')].find(b => (b.innerText || '').trim() === '스페이스');
      if (toggle) toggle.click();
    });
    await sleep(600);
    if (!(await find())) { emit('source', { name, source_kind: 'space', error: 'not-found' }); return; }
  }
  await sleep(1500);
  await closeInvite();
  const where = await tab.evaluate(() => location.hash);
  if (!where.startsWith('#/space/')) { emit('source', { name, source_kind: 'space', error: 'not-opened' }); return; }
  const expected = await tab.evaluate(() => {
    const m = document.body.innerText.match(/모든 항목\s*([\d,]+)개의 노트/);
    return m ? Number(m[1].replace(/,/g, '')) : null;
  });
  report(name, 'space', where.slice('#/space/'.length).split(/[/?]/)[0], expected, await collect('space', expected, 0, 0));
}
try {
  if (!(await appReady())) {
    emit('error', { code: tab.url().startsWith(ARGS.url) ? 'app-not-ready' : 'not-signed-in' });
  } else if (ARGS.kind === 'notebook') {
    await notebook(ARGS.name);
  } else {
    await space(ARGS.name);
  }
} finally {
  await closeTab(tab);
}
"""

READ_SCRIPT = _PRELUDE + r"""
async function editor() {
  const frame = tab.frames().find(f => f.name() === 'qa-COMMON_EDITOR_IFRAME');
  if (!frame) return null;
  const inner = await frame.evaluate(() => {
    const body = document.getElementById('en-note');
    const title = document.querySelector('textarea');
    return body ? { title: title ? title.value.trim() : '', text: body.innerText } : null;
  }).catch(() => null);
  if (!inner) return null;
  const updated = await tab.evaluate(() => { const e = document.getElementById('qa-NOTE_EDIT_INFO'); return e ? e.innerText.trim() : null; });
  return Object.assign(inner, { updated, loading: inner.text.includes('불러오는 중') });
}
try {
  if (!(await appReady())) {
    emit('error', { code: tab.url().startsWith(ARGS.url) ? 'app-not-ready' : 'not-signed-in' });
  } else {
    const started = Date.now();
    let previous = null, previousBody = null;
    // The call starts on the notebook list, where no note is open. If an editor is on the page anyway, what it shows
    // is the baseline: the first note must differ from it too (a title can switch before its body; review 2026-11-01).
    await sleep(800);
    const base = await editor();
    if (base) { previous = base.title + '\u0000' + base.text; previousBody = base.text; }
    for (const want of ARGS.notes) {
      if (Date.now() - started > ARGS.budget_ms) { emit('stopped', { guid: want.guid }); break; }
      await tab.evaluate(h => { location.hash = h; }, '#/notes/' + want.guid);
      let got = null, last = null;
      for (let k = 0; k < 32 && !got; k++) {
        await sleep(250);
        const now = await editor();
        if (!now || now.loading) { last = null; continue; }
        const key = now.title + '\u0000' + now.text;
        const titled = want.title === null || now.title.split(/\s+/).filter(Boolean).join(' ') === want.title;
        const steady = key === last;
        last = key;
        // The title can switch before the body, so a body equal to the previous note's is never taken (however long
        // it stays): the note is reported unread and Smith retries it in another order, keeping its earlier copy.
        if (titled && steady && key !== previous && (previousBody === null || now.text !== previousBody)) got = now;
      }
      if (!got) { emit('unread', { guid: want.guid }); continue; }
      previous = got.title + '\u0000' + got.text;
      previousBody = got.text;
      emit('note', { guid: want.guid, title: got.title, text: got.text.slice(0, ARGS.max_text), updated: got.updated });
    }
  }
} finally {
  await closeTab(tab);
}
"""


# --- listing and reading ---------------------------------------------------------------------------------------------

def list_notes(notebooks: list[str], spaces: list[str], *, runner: Runner, executable: Path) -> tuple[list[Listed], dict[str, dict[str, Any]]]:
    """Every note of the allowed sources, one call per source. Returns (notes, per-source report).
    A source that fails is reported with its error code; EvernoteError only when Aside or the sign-in fails."""
    listed: dict[str, Listed] = {}
    report: dict[str, dict[str, Any]] = {}
    for kind, names in (("notebook", notebooks), ("space", spaces)):
        for name in names:
            rows, report[name] = _list_source(kind, name, runner, executable)
            for row in rows:
                listed.setdefault(row.guid, row)
    return list(listed.values()), report


def _list_source(kind: str, name: str, runner: Runner, executable: Path) -> tuple[list[Listed], dict[str, Any]]:
    """One source's notes over as many calls as its list needs (each stops at its budget and reports where it was;
    the next starts there). Returns (rows, {"kind", "expected", "found", "error"})."""
    args: dict[str, Any] = {"url": WEB, "kind": kind, "name": name, "guid": None, "start": 0, "have": 0,
                            "expected": None, "budget_ms": CALL_BUDGET_MS}
    rows: dict[str, Listed] = {}
    error = None
    for _ in range(LIST_CALLS):
        try:
            records = _call(LIST_SCRIPT, args, runner, executable)
        except EvernoteError as failure:
            if failure.code in ("aside-not-found", "not-signed-in", "aside-failed"):
                raise
            error = failure.code
            break
        for r in records:
            guid = str(r.get("guid") or "")
            if r.get("kind") == "listed" and _GUID.fullmatch(guid):
                title = r.get("title")
                rows.setdefault(guid, Listed(guid, name, kind, None if title is None else str(title)))
        summary = next((r for r in records if r.get("kind") == "source"), {"error": "no-summary"})
        args["expected"] = args["expected"] if args["expected"] is not None else summary.get("expected")
        error = summary.get("error")
        resume = summary.get("resume")
        if error or not isinstance(resume, (int, float)) or not _GUID.fullmatch(str(summary.get("guid") or "")):
            break
        args.update(guid=summary["guid"], start=resume, have=len(rows))
    else:
        error = error or "list-unfinished"
    return list(rows.values()), {"kind": kind, "expected": args["expected"], "found": len(rows), "error": error}


_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_UNTITLED = {"", "제목 없음", "Untitled"}


def expected_title(listed: str | None) -> str | None:
    """The title the editor must show, whitespace-collapsed: "" for the placeholder of an untitled note (its editor
    title field is empty); None (only the content-change check) when the list gave none."""
    if listed is None:
        return None
    title = " ".join(listed.split())
    return "" if title in _UNTITLED else title


def read_notes(wanted: list[Listed], *, runner: Runner, executable: Path) -> dict[str, dict[str, Any]]:
    """Open each note and read it; returns {guid: {"title", "text", "updated_label"}} for the notes read cleanly.
    A note the editor did not show (wrong title, a body equal to the note before it, loading) is missing from the
    result after one retry of the missed notes in reverse order (each then follows a different note)."""
    got = _read_pass(wanted, runner=runner, executable=executable)
    missed = [n for n in wanted if n.guid not in got]
    if missed:
        got.update(_read_pass(missed[::-1], runner=runner, executable=executable))
    return got


def _read_pass(wanted: list[Listed], *, runner: Runner, executable: Path) -> dict[str, dict[str, Any]]:
    got: dict[str, dict[str, Any]] = {}
    pending = list(wanted)
    calls = 0
    while pending and calls < 2 * (len(wanted) // BATCH + 1):
        calls += 1
        batch = pending[:BATCH]
        args = {"url": f"{WEB}#/notebooks", "budget_ms": CALL_BUDGET_MS, "max_text": MAX_TEXT,
                "notes": [{"guid": n.guid, "title": expected_title(n.title)} for n in batch]}
        try:
            records = _call(READ_SCRIPT, args, runner, executable)
        except EvernoteError as error:
            if error.code in ("aside-not-found", "not-signed-in"):
                raise
            logger.warning("evernote read batch failed (%s); %d notes left", error.code, len(pending))
            pending = pending[len(batch):]
            continue
        stopped = next((str(r.get("guid")) for r in records if r.get("kind") == "stopped"), None)
        for r in records:
            if r.get("kind") == "note" and isinstance(r.get("text"), str):
                got[str(r["guid"])] = {"title": str(r.get("title") or ""), "text": r["text"],
                                       "updated_label": str(r.get("updated") or "")}
        cut = next((i for i, n in enumerate(batch) if n.guid == stopped), len(batch))
        pending = batch[cut:] + pending[len(batch):] if stopped else pending[len(batch):]
    return got


# --- sync ------------------------------------------------------------------------------------------------------------

def sync(folder: Path, notebooks: list[str], spaces: list[str], *, now: datetime, today: date,
         runner: Runner = run_aside, executable: Path | None = None) -> dict[str, Any]:
    """List and read the allowed sources and rewrite `notes.json`.

    Returns {"outcome": "synced" | "partial" | "error", "notes", "read", "kept", "unread", "sources", "error_code"}.
    Errors are returned, not raised. A note that could not be read keeps its earlier copy; a source that failed or
    listed fewer notes than it shows keeps every earlier note of it, so deletions only take effect on a clean listing.
    """
    with claimed(folder) as mine:
        if not mine:
            return {"outcome": "busy", "notes": len(load_raw(folder)), "read": 0, "kept": 0, "unread": 0, "sources": {},
                    "error_code": "already-running"}
        return _sync_claimed(folder, notebooks, spaces, now=now, today=today, runner=runner, executable=executable)


def _sync_claimed(folder: Path, notebooks: list[str], spaces: list[str], *, now: datetime, today: date,
                  runner: Runner, executable: Path | None) -> dict[str, Any]:
    state = sync_state(folder)
    try:
        exe = executable or find_aside()
        listed, report = list_notes(notebooks, spaces, runner=runner, executable=exe)
        # The cap bounds the reading only: every listed GUID still counts, so nothing is dropped for being past it.
        read = read_notes(listed[:MAX_NOTES], runner=runner, executable=exe)
    except EvernoteError as error:
        _write_state(folder, {**state, "last_checked": now.isoformat(), "last_outcome": "error", "error_code": error.code})
        return {"outcome": "error", "notes": len(load_raw(folder)), "read": 0, "kept": 0, "unread": 0, "sources": {},
                "error_code": error.code}
    previous = {n["guid"]: n for n in load_raw(folder)}
    stored, kept, unread = merge(listed, read, previous, report, now=now, today=today)
    _write_notes(folder, stored, now)
    incomplete = any(r.get("error") or r.get("expected") is None or (r.get("found") or 0) < r["expected"]
                     for r in report.values())
    outcome = "partial" if (unread or incomplete) else "synced"
    _write_state(folder, {"last_checked": now.isoformat(), "last_outcome": outcome, "sources": report,
                          "count": len(stored), "unread": unread})
    logger.info("evernote sync %s: %d notes (%d read, %d kept, %d unread)", outcome, len(stored), len(read), kept, unread)
    return {"outcome": outcome, "notes": len(stored), "read": len(read), "kept": kept, "unread": unread,
            "sources": report, "error_code": None}


@contextmanager
def claimed(folder: Path) -> Iterator[bool]:
    """An OS lock on `sync.lock` for the sync (the scheduled run and a manual `smith evernote sync` must not both
    write). Yields False when another process holds it. The OS drops the lock with its process, so a crashed run
    never blocks the next and there is no stale claim to take over (review 2026-11-01)."""
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / LOCK_FILE, "a+b") as handle:
        try:
            _lock(handle, True)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            _lock(handle, False)


def _lock(handle: Any, take: bool) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if take else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if take else fcntl.LOCK_UN)


def running(folder: Path) -> bool:
    """Whether another process is syncing now."""
    with claimed(folder) as mine:
        return not mine


def merge(listed: list[Listed], read: dict[str, dict[str, Any]], previous: dict[str, dict[str, Any]],
          report: dict[str, dict[str, Any]], *, now: datetime, today: date) -> tuple[list[dict[str, Any]], int, int]:
    """The new store: fresh reads, earlier copies of unread notes, and every earlier note of an incomplete source.
    Returns (notes, kept, unread): `unread` counts every listed note not read this time, kept copy or not."""
    notes: dict[str, dict[str, Any]] = {}
    kept = unread = 0
    for item in listed:
        fresh = read.get(item.guid)
        if fresh is not None:
            notes[item.guid] = {"guid": item.guid, "source": item.source, "source_kind": item.kind, "title": fresh["title"],
                                "text": clean(fresh["text"]), "updated_label": fresh["updated_label"],
                                "updated": _iso(parse_updated(fresh["updated_label"], today)), "read_at": now.isoformat()}
            continue
        unread += 1
        if item.guid in previous:
            notes[item.guid] = {**previous[item.guid], "source": item.source, "source_kind": item.kind}
            kept += 1
    complete = {name for name, r in report.items()
                if not r.get("error") and r.get("expected") is not None and (r.get("found") or 0) >= r["expected"]}
    for guid, old in previous.items():
        if guid not in notes and old.get("source") in report and old.get("source") not in complete:
            notes[guid] = old
            kept += 1
    return list(notes.values()), kept, unread


def clean(text: str) -> str:
    """The note body without editor controls, with secret-marked lines replaced. A secret's value can sit on the
    next line, so a note with any marker is also kept from every model (`sensitive`)."""
    lines = []
    for line in text.replace("​", "").replace("\xa0", " ").splitlines():
        stripped = line.strip()
        if stripped in _UI_LINES:
            continue
        lines.append(REDACTED if _SECRET_LINE.search(stripped) else line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def masked(text: str) -> str:
    """The text a model may get: long numbers and long key-like strings replaced."""
    return _LONG_KEY.sub("[값 가림]", _LONG_NUMBER.sub("[숫자 가림]", text))


def sensitive(title: str, text: str) -> bool:
    """A note that names a password, account or card number, ID number or code anywhere (title or body)."""
    return bool(_SECRET_LINE.search(title) or _SECRET_LINE.search(text) or REDACTED in text)


def parse_updated(label: str, today: date) -> date | None:
    """The date in Evernote's "수정됨 …" line: "2024년 12월 28일", "11월 1일" (this year), "25. 12. 30.", 오늘/어제/N시간 전."""
    if not label:
        return None
    if any(word in label for word in ("오늘", "방금", "분 전", "시간 전")):
        return today
    if "어제" in label:
        return today - timedelta(days=1)
    try:
        if match := _FULL.search(label):
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if match := _DOTS.search(label):
            return date(2000 + int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if match := _MONTH_DAY.search(label):
            on = date(today.year, int(match.group(1)), int(match.group(2)))
            return on if on <= today else on.replace(year=today.year - 1)
    except ValueError:
        return None
    return None


def _iso(on: date | None) -> str | None:
    return None if on is None else on.isoformat()


# --- storage ---------------------------------------------------------------------------------------------------------

def load_raw(folder: Path) -> list[dict[str, Any]]:
    path = folder / NOTES_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return []
    items = data.get("notes", []) if isinstance(data, dict) else []
    return [n for n in items if isinstance(n, dict) and isinstance(n.get("guid"), str) and isinstance(n.get("text"), str)]


def notes(folder: Path, *, spaces: bool = True) -> list[keep.Note]:
    """The stored notes as Keep notes (id "evernote:<guid>", labels: source name, "evernote", the source kind).
    `spaces=False` keeps notebook notes only: spaces hold book and lecture summaries and travel plans, whose words
    are mostly not the client's own and must not become the sentence of the day. Sensitive notes are never returned:
    everything this gives can reach a model."""
    found = []
    for item in load_raw(folder):
        if (not spaces and item.get("source_kind") == "space") or sensitive(str(item.get("title", "")), item["text"]):
            continue
        try:
            on = date.fromisoformat(item["updated"]) if item.get("updated") else None
        except (TypeError, ValueError):
            on = None
        edited = None if on is None else datetime.combine(on, time(12), tzinfo=timezone.utc)
        kind_label = SPACE_LABEL if item.get("source_kind") == "space" else NOTEBOOK_LABEL
        found.append(keep.Note(f"evernote:{item['guid']}", str(item.get("title", "")), masked(item["text"]),
                               (str(item.get("source", "")), LABEL, kind_label), edited, False, False, "note"))
    return found


def _write_notes(folder: Path, items: list[dict[str, Any]], now: datetime) -> None:
    _replace(folder / NOTES_FILE, {"synced_at": now.isoformat(), "notes": items})


def sync_state(folder: Path) -> dict[str, Any]:
    path = folder / SYNC_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(folder: Path, state: dict[str, Any]) -> None:
    _replace(folder / SYNC_FILE, state)


def _replace(path: Path, payload: dict[str, Any]) -> None:
    """Atomic write through a temporary file of this writer's own (never a name another writer could share)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def sync_due(folder: Path, *, now: datetime, local_now: datetime, at: time) -> bool:
    """Once a local day, at or after `at`, unless another process is syncing now. A day whose run failed (Aside closed, Evernote signed out) is tried again every RETRY_AFTER."""
    if running(folder):
        return False
    state = sync_state(folder)
    try:
        checked = state.get("last_checked")
        last = None if not checked else datetime.fromisoformat(checked)
    except (TypeError, ValueError):
        return True
    if local_now.time() < at:
        return False
    if last is None or last.astimezone(local_now.tzinfo).date() != local_now.date():
        return True
    return state.get("last_outcome") == "error" and last <= now - RETRY_AFTER
