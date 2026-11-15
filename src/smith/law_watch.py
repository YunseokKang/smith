"""Tax-law watch (requirement FR-54): whether the laws Smith's tax rules rest on changed after the rules were checked.

Smith's tax rules are constants checked against the law on a date (`smith.tax.RULES_CHECKED`,
`smith.holding_tax.RULES_CHECKED`). Once a week this asks the 법제처 national law information API (국가법령정보
공동활용, the client's own OC key) for each watched law's versions in force and scheduled (`lawSearch.do`,
target=eflaw, nw=2,3; parameters and fields from the official guide `lsEfYdListGuide`, read 2026-11-15) and keeps the
ones promulgated after the date the rules relying on that law were checked: an amendment Smith's figures do not yet
reflect. The tax day then names each with its promulgation and effective dates, and its source line says when the
law was last checked. The rules themselves are updated by a developer, never by the watch, and no model reads
anything here.

The result is kept beside the ledger in `data/law-watch.json` (ignored by Git). A failure is kept as a failure, so
the report never says "no amendment" for a law it could not read.
"""
import json
import os
import urllib.parse
import uuid
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from smith.net import open_url

SEARCH = "https://www.law.go.kr/DRF/lawSearch.do"
ARTICLE = "https://www.law.go.kr/DRF/lawService.do"
STORE = "law-watch.json"
STALE = timedelta(days=7)
RETRY = timedelta(days=1)
_TIMEOUT_SECONDS = 20
_DISPLAY = 100
_MAX_PAGES = 5

# (law name exactly as 법령명한글, what Smith computes from it, the rule set it was checked for). Names are those of
# the laws the tax and holding-tax rules cite.
WATCHED = (
    ("소득세법", "연금계좌 세액공제·근로소득세·양도소득세·금융소득 종합과세", "tax"),
    ("소득세법 시행령", "1세대1주택 비과세·고가주택·저축성보험 비과세", "tax"),
    ("조세특례제한법", "ISA 등 조세특례", "tax"),
    ("조세특례제한법 시행령", "ISA 등 조세특례", "tax"),
    ("상속세 및 증여세법", "혼인·출산 증여재산 공제", "tax"),
    ("지방세법", "재산세", "holding"),
    ("지방세법 시행령", "재산세 공정시장가액비율·1세대1주택", "holding"),
    ("종합부동산세법", "종합부동산세", "holding"),
    ("종합부동산세법 시행령", "종합부동산세 공정시장가액비율·혼인 특례", "holding"),
    ("농어촌특별세법", "종합부동산세에 붙는 농어촌특별세", "holding"),
)

Getter = Callable[[str], tuple[int, bytes]]


class LawWatchError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def urllib_get(url: str) -> tuple[int, bytes]:
    with open_url(url, timeout=_TIMEOUT_SECONDS) as response:
        return response.status, response.read()


def checked_on(rules: str) -> date:
    """The day the rule set was last checked against the law."""
    from smith import holding_tax, tax

    return date.fromisoformat({"tax": tax.RULES_CHECKED, "holding": holding_tax.RULES_CHECKED}[rules])


def _document(url: str, get: Getter) -> ElementTree.Element:
    try:
        status, body = get(url)
    except OSError:
        raise LawWatchError("network-error") from None
    if status != 200:
        raise LawWatchError(f"http-{status}")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        text = body.decode("utf-8", errors="replace")
        raise LawWatchError("auth-failed" if ("검증" in text or "인증" in text) else "invalid-response") from None
    # A call from an address the OC was not registered for is answered in XML (seen live 2026-11-15):
    # <Response><result>사용자 정보 검증에 실패하였습니다.</result><msg>… 서버장비의 IP주소 및 도메인주소를 등록해 주세요.</msg>.
    if root.tag == "Response" and root.find("result") is not None:
        message = (root.findtext("result") or "") + (root.findtext("msg") or "")
        raise LawWatchError("auth-failed" if ("검증" in message or "인증" in message) else "api-error")
    return root


def versions(name: str, oc: str, *, get: Getter = urllib_get) -> list[dict[str, Any]]:
    """The versions of the law named exactly `name` that are in force or scheduled (newest promulgation first).
    The OC is sent to the API only and never logged.

    Raises:
        LawWatchError: network-error, http-<status>, auth-failed (the key, or the calling PC's address not registered
            with it), api-error, invalid-response, incomplete, invalid-row, no-match.
    """
    found: list[dict[str, Any]] = []
    seen = 0
    for page in range(1, _MAX_PAGES + 1):
        query = urllib.parse.urlencode({"OC": oc, "target": "eflaw", "type": "XML", "query": name, "nw": "2,3",
                                        "display": _DISPLAY, "page": page, "sort": "ddes"})
        root = _document(f"{SEARCH}?{query}", get)
        try:
            total = int((root.findtext(".//totalCnt") or "").strip())
        except ValueError:
            raise LawWatchError("invalid-response") from None
        rows = [item for item in root.iter() if item.find("법령명한글") is not None]
        # Every page but the last is full, and the last holds the rest (Codex review 2026-11-15: a short answer
        # missing the newest amendment read as "no amendment" for a week).
        if len(rows) != min(_DISPLAY, max(0, total - seen)):
            raise LawWatchError("incomplete")
        seen += len(rows)
        for item in rows:
            if (item.findtext("법령명한글") or "").strip() != name:
                continue
            text = lambda tag: (item.findtext(tag) or "").strip()  # noqa: E731
            try:
                promulgated, effective = _day(text("공포일자")), _day(text("시행일자"))
            except ValueError:
                raise LawWatchError("invalid-row") from None
            # The API's detail link carries the OC (Codex review 2026-11-15): never kept; the public page instead.
            found.append({"name": name, "serial": text("법령일련번호"), "law_id": text("법령ID"),
                          "promulgated": promulgated.isoformat(), "number": text("공포번호"), "kind": text("제개정구분명"),
                          "effective": effective.isoformat(), "status": text("현행연혁코드"),
                          "link": f"https://www.law.go.kr/법령/{urllib.parse.quote(name)}"})
        if seen >= total:
            break
    else:
        raise LawWatchError("incomplete")
    if not found:
        raise LawWatchError("no-match")
    return sorted(found, key=lambda v: (v["promulgated"], v["number"]), reverse=True)


def _day(value: str) -> date:
    if not (len(value) == 8 and value.isdigit()):
        raise ValueError(value)
    return date(int(value[:4]), int(value[4:6]), int(value[6:]))


def check(oc: str | None, *, now: datetime, get: Getter = urllib_get,
          previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Every watched law's versions, or its failure code; "no-key" for all when the OC is not stored.

    A law that fails keeps the versions of its last good reading (`read_at` says when) beside this week's error, so
    an amendment found before is not forgotten because one week's call failed (Codex review 2026-11-15).

    Raises:
        LawWatchError: secret-in-result, should the key ever appear in what would be stored.
    """
    before = (previous or {}).get("laws") or {}
    laws: dict[str, Any] = {}
    refused = False   # Once the key is refused, the other laws would be refused the same way: they are not asked.
    for name, about, rules in WATCHED:
        last = before.get(name) or {}
        entry: dict[str, Any] = {"about": about, "rules": rules, "versions": list(last.get("versions") or []),
                                 "read_at": last.get("read_at"), "error": None}
        if not oc:
            entry["error"] = "no-key"
        elif refused:
            entry["error"] = "auth-failed"
        else:
            try:
                entry["versions"], entry["read_at"] = versions(name, oc, get=get), now.isoformat()
            except LawWatchError as error:
                entry["error"] = error.code
                refused = error.code == "auth-failed"
        laws[name] = entry
    result = {"checked_at": now.isoformat(), "laws": laws}
    written = json.dumps(result, ensure_ascii=False)
    if oc and (oc in written or urllib.parse.quote(oc) in written or urllib.parse.quote_plus(oc) in written):
        raise LawWatchError("secret-in-result")
    return result


def pending(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Amendments promulgated after the day their rule set was checked, oldest first: what the figures may not
    reflect yet."""
    found = []
    for name, entry in ((result or {}).get("laws") or {}).items():
        since = checked_on(entry["rules"]).isoformat()
        found.extend({**v, "about": entry["about"], "rules": entry["rules"]}
                     for v in entry.get("versions") or [] if v["promulgated"] > since)
    return sorted(found, key=lambda v: (v["promulgated"], v["name"]))


def summary(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """What a report shows: when the laws were read, which could not be, and the amendments not reflected yet."""
    if not result:
        return None
    laws = result.get("laws") or {}
    failed = sorted(name for name, entry in laws.items() if entry.get("error"))
    return {"checked_at": result.get("checked_at"), "failed": failed,
            "no_key": bool(laws) and all(entry.get("error") == "no-key" for entry in laws.values()),
            "pending": pending(result)}


def load(folder: Path) -> dict[str, Any] | None:
    try:
        found = json.loads((folder / STORE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) and isinstance(found.get("laws"), dict) else None


def save(folder: Path, result: dict[str, Any]) -> None:
    """Write `result` merged into what is stored, under an OS file lock: `smith law check` and the scheduled check
    may overlap, and an older failed attempt must not overwrite a newer good reading or the amendments it found
    (Codex review 2026-11-15). Per law the newer good reading wins; this attempt's error stays only when it came
    after that reading."""
    from smith.memory import _locked   # The same OS lock the client memory and the official-price store use.

    folder.mkdir(parents=True, exist_ok=True)
    with _locked(folder):
        stored = load(folder)
        merged = _merge(stored, result) if stored else result
        temporary = folder / f"{STORE}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(temporary, folder / STORE)
        finally:
            temporary.unlink(missing_ok=True)


def _merge(stored: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    attempted = result.get("checked_at") or ""
    laws = {}
    for name, entry in result["laws"].items():
        old = (stored.get("laws") or {}).get(name) or {}
        if (old.get("read_at") or "") > (entry.get("read_at") or ""):
            entry = {**entry, "versions": old.get("versions") or [], "read_at": old["read_at"],
                     "error": entry.get("error") if attempted > old["read_at"] else old.get("error")}
        laws[name] = entry
    return {"checked_at": max(attempted, stored.get("checked_at") or ""), "laws": laws}


def store_for(conn: Any) -> dict[str, Any] | None:
    """The stored result beside the ledger file `conn` is open on; None for an in-memory ledger."""
    import sqlite3

    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    path = next((row[2] for row in rows if row[1] == "main"), "")
    return load(Path(path).parent) if path else None


def stale(folder: Path, now: datetime) -> bool:
    """Due when never run, a week after a full read, or a day after a read with any failure."""
    result = load(folder)
    if result is None:
        return True
    try:
        last = datetime.fromisoformat(result["checked_at"])
    except (KeyError, TypeError, ValueError):
        return True
    failed = any(entry.get("error") for entry in result["laws"].values())
    return now - last > (RETRY if failed else STALE)


def article(name: str, number: str, oc: str, *, get: Getter = urllib_get) -> list[str]:
    """The text of one article of the law in force (development aid: read a rule before encoding it). `number` is
    "110" or "110의2". Returns the article, paragraph and item texts in order.

    Raises:
        LawWatchError: as `versions`, plus bad-article.
    """
    main, _, branch = number.partition("의")
    if not (main.isdigit() and (not branch or branch.isdigit())):
        raise LawWatchError("bad-article")
    current = [v for v in versions(name, oc, get=get) if v["effective"] <= date.today().isoformat()]
    if not current:
        raise LawWatchError("no-match")
    version = max(current, key=lambda v: v["effective"])
    query = urllib.parse.urlencode({"OC": oc, "target": "eflawjosub", "type": "XML", "MST": version["serial"],
                                    "efYd": version["effective"].replace("-", ""),
                                    "JO": f"{int(main):04d}{int(branch or 0):02d}"})
    root = _document(f"{ARTICLE}?{query}", get)
    texts = [(e.text or "").strip() for e in root.iter() if e.tag in ("조문내용", "항내용", "호내용", "목내용")]
    return [t for t in texts if t]
