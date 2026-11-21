"""Samsung Card statements read from the e-mailed secure statement (FR-60, docs/spending-design.md).

The statement mail carries a VestMail attachment: an HTML file whose payload is SEED-CBC encrypted under a key
derived from the member's birth date. Decrypted, it is not the statement but a form that posts a one-time value to
`bill.samsungcard.com`; the statement itself is a page that the site's own script renders. So the adapter

1. decrypts the attachment in Python (`login_form`),
2. opens that form in a headless Chrome with a throwaway profile, driven over the DevTools protocol (`Session`), and
3. reads the rendered rows, and the earlier statements the page offers under "지난 명세서 보기" (`read`).

Read-only: the adapter loads statement pages and presses only "더보기" and the past-statement selector. It never
touches payment controls (즉시결제, 분할납부, 리볼빙). The site may change its markup at any time; a statement whose
rows do not add up to the page's own total is refused by `spending.parse`, not stored.

Verified by hand on 2026-11-21 against the 2026-11 statement and two earlier ones.
"""
import base64
import hashlib
import html
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HOST = "https://bill.samsungcard.com/"
SENDER = "billing@samsungcard.com"
ATTACHMENT = re.compile(r"samsungcard_detail_(\d{8})\.html")
CHROME_PATHS = (Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"))
_BLOCK = re.compile(r's\[\d+\]\s*=\s*"([A-Za-z0-9+/=]+)"')
_FIELD = re.compile(r'name=(\w+)\s+value="?([0-9A-Za-z+/=_-]+)"?')
_PORT = 9344
_PAGE_WAIT = 45       # seconds for a statement page to render
_MORE_CLICKS = 80     # ten rows a click: 800 rows a section at most

# Runs in the statement page: presses "더보기" until a section stops growing, then returns every row as text.
# A section's total is read before the first press: the page zeroes it once more rows are appended (seen 2026-11-21).
_EXTRACT = r"""(async function(){
const sleep=ms=>new Promise(r=>setTimeout(r,ms)), text=e=>e?e.textContent.replace(/\s+/g,' ').trim():'';
const lists=[...document.querySelectorAll('ul[id^="inquire_append_"]')], sections=[];
for(const ul of lists){
  const key=ul.id.replace('inquire_append_',''), count=()=>ul.querySelectorAll('li.li:not(.top_total)').length;
  const total=text(ul.querySelector('li.top_total .total strong')), stated=text(ul.querySelector('li.top_total .num strong'));
  for(let i=0,still=0;i<%d&&still<2;i++){
    const more=[...document.querySelectorAll('#nextData'+key)].find(b=>b.offsetParent!==null);
    if(!more)break; const before=count(); more.click(); await sleep(1500); still=count()>before?0:still+1;}
  const rows=[...ul.querySelectorAll('li.li:not(.top_total)')].map(li=>{
    const store=li.querySelector('.store_2');
    return {merchant:store?(store.getAttribute('title')||store.textContent).trim():'',
            amount:text(li.querySelector('.abs_em strong')),
            date:text(li.querySelector('.td.date .ls0')).replace('이용일자','').trim(),
            detail:text(li.querySelector('.desc_wrap')).slice(0,400)};});
  if(rows.length) sections.push({key,total,count:stated,rows});}
const id=i=>text(document.getElementById(i));
return JSON.stringify({period:id('crdsLdPtrmStrtdt2_pc'),written:id('spcshWkDt'),sections,
  earlier:[...document.querySelectorAll('#selectBill option')].map(o=>o.value).filter(v=>/^\d{8}$/.test(v))});})()""" % _MORE_CLICKS


class StatementError(Exception):
    """A statement could not be read. `code` is safe to log and to show."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def login_form(attachment: str, birth: str) -> dict[str, str]:
    """The one-time form fields inside a statement attachment, decrypted with the birth date (YYMMDD).

    VestMail (read from the attachment's own script): key = SHA-256(SHA-256(password))[:16], IV = SHA-256(password)[:16],
    SEED-CBC with PKCS#7 padding; each block is base64 with a 16-byte header, and the plaintext starts with the key
    itself, which is how a wrong password is told from a damaged file.
    """
    from cryptography.hazmat.decrepit.ciphers.algorithms import SEED
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    once = hashlib.sha256(birth.encode()).digest()
    key, iv = hashlib.sha256(once).digest()[:16], once[:16]
    blocks = _BLOCK.findall(attachment)
    if not blocks:
        raise StatementError("attachment-format")
    plain = b""
    for block in blocks:
        try:
            cipher = base64.b64decode(block)[16:]
            decryptor = Cipher(SEED(key), modes.CBC(iv)).decryptor()
            text = decryptor.update(cipher) + decryptor.finalize()
        except ValueError:
            raise StatementError("attachment-format") from None
        if text[:16] != key:
            raise StatementError("wrong-password")
        plain += text[16:len(text) - text[-1]]
    page = plain.decode("utf-8", "replace")
    action = re.search(r'action="([^"]+)"', page)
    fields = dict(_FIELD.findall(page))
    # The form goes to the card company's statement host and nowhere else, whatever the attachment says.
    if not action or not action.group(1).startswith(HOST) or "pvalue" not in fields:
        raise StatementError("attachment-format")
    return {"action": action.group(1), **fields}


def find_chrome() -> Path | None:
    return next((path for path in CHROME_PATHS if path.is_file()), None)


class Session:
    """A headless Chrome with a throwaway profile, driven over the DevTools protocol. Close it when done."""

    def __init__(self, chrome: Path, port: int = _PORT) -> None:
        import websocket

        self._profile = tempfile.mkdtemp(prefix="smith-card-")
        self._process = subprocess.Popen(
            [str(chrome), "--headless=new", "--disable-gpu", "--no-first-run", "--disable-extensions",
             f"--user-data-dir={self._profile}", f"--remote-debugging-port={port}", "--remote-allow-origins=*",
             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._socket = None
        self._sent = 0
        try:
            self._socket = websocket.create_connection(self._page_socket(port), timeout=_MORE_CLICKS * 2 + 60)
        except (OSError, websocket.WebSocketException, StatementError):
            self.close()
            raise StatementError("chrome-unavailable") from None

    def _page_socket(self, port: int) -> str:
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as response:
                    pages = [t for t in json.load(response) if t.get("type") == "page"]
                if pages:
                    return str(pages[0]["webSocketDebuggerUrl"])
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(0.3)
        raise StatementError("chrome-unavailable")

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self._sent += 1
        self._socket.send(json.dumps({"id": self._sent, "method": method, "params": params}))
        while True:
            message = json.loads(self._socket.recv())
            if message.get("id") == self._sent:
                return message.get("result") or {}

    def evaluate(self, expression: str) -> Any:
        """The value of a JavaScript expression in the page (promises awaited); None when it threw."""
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        return (result.get("result") or {}).get("value")

    def open_form(self, form: dict[str, str]) -> None:
        """Post the one-time form, as opening the attachment in a browser would."""
        inputs = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
                         for k, v in form.items() if k != "action")
        page = Path(self._profile) / "open.html"
        page.write_text(f'<!doctype html><meta charset="utf-8"><form name="f" method="POST" '
                        f'action="{html.escape(form["action"])}">{inputs}</form><script>document.f.submit()</script>',
                        encoding="utf-8")
        self.call("Page.enable")
        self.call("Page.navigate", url=page.as_uri())

    def close(self) -> None:
        try:
            if self._socket is not None:
                self.call("Browser.close")
                self._socket.close()
        except Exception:   # noqa: BLE001 - shutting down: the process is killed below whatever the socket did
            pass
        try:
            self._process.wait(10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(10)
        for _ in range(10):   # Chrome's helper processes let go of the profile a moment after the parent exits.
            shutil.rmtree(self._profile, ignore_errors=True)
            if not Path(self._profile).exists():
                break
            time.sleep(0.5)


def read(session: Session, form: dict[str, str], settled: str, *, earlier: int = 0,
         known: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """The mailed statement and up to `earlier` statements before it that are not in `known`, newest first.

    `settled` is the mailed statement's payment day (YYYYMMDD, from the attachment's name). Each result is the raw page
    reading: {"settled", "period", "written", "sections": [{"key", "total", "count", "rows": [...]}]}.
    """
    session.open_form(form)
    current = _wait(session, "")
    results = [] if settled in known else [{"settled": settled, **_without_earlier(current)}]
    wanted = [day for day in current.get("earlier", []) if day < settled and day not in known][:earlier]
    period = current["period"]
    for day in wanted:
        # The page's own selector: choosing a statement reloads the page for that payment day.
        session.evaluate(f"(function(){{var s=$('#selectBill');s.val('{day}');s.trigger('change');}})()")
        page = _wait(session, period)
        period = page["period"]
        results.append({"settled": day, **_without_earlier(page)})
    return results


def _wait(session: Session, previous_period: str) -> dict[str, Any]:
    """The statement page once it shows a period other than the one before."""
    for _ in range(_PAGE_WAIT):
        time.sleep(1)
        period = session.evaluate("(document.getElementById('crdsLdPtrmStrtdt2_pc')||{}).textContent||''")
        if isinstance(period, str) and period.strip() and period.strip() != previous_period:
            time.sleep(3)   # the row lists fill in just after the header
            raw = session.evaluate(_EXTRACT)
            if isinstance(raw, str):
                return json.loads(raw)
            break
    raise StatementError("page-not-rendered")


def _without_earlier(page: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in page.items() if key != "earlier"}
