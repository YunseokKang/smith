import base64
import json
import subprocess
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from smith import gmail, ledger, qa
from smith.importer import parse_import
from smith.report_data import build_report

AT = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
ME = "me@example.com"


def b64(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def message(message_id, *, sender=ME, labels=("SENT", "INBOX"), text="비상금은 어디에 두는 게 좋을까요?\n\n"
            "2026년 10월 5일 (월) 오전 6:00, Smith님이 작성:\n> 원문"):
    return {"id": message_id, "threadId": "thread0001", "labelIds": list(labels), "internalDate": "1791172800000",
            "payload": {"mimeType": "multipart/alternative",
                        "headers": [{"name": "From", "value": f"Me <{sender}>"},
                                    {"name": "Message-ID", "value": f"<{message_id}@mail>"}],
                        "parts": [{"mimeType": "text/plain", "body": {"data": b64(text)}},
                                  {"mimeType": "text/html", "body": {"data": b64("<p>x</p>")}}]}}


class QaTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "smith.db"
        doc = {"schema_version": 1, "import_id": "m1", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}], "records": [
                   {"id": "cash", "kind": "asset", "owner_id": "self", "effective_at": AT.isoformat(), "revision": 1,
                    "status": "active", "category": "cash", "account_type": "bank", "currency": "KRW", "value": "10000000",
                    "valuation_method": "manual", "liquidity": "immediate"}]}
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=AT)
            ledger.claim_report(conn, report_id="r1", slot=None, kind="monday", trigger="manual", created_at=AT, as_of=AT,
                                baseline=None, missed_slots=[])
            ledger.finish_report(conn, report_id="r1", status="sent", finished_at=AT, subject="[Smith] 10/05 월요 종합 보고",
                                 message_id="reportmsg01")
        smith_answer = message("lostanswer01")
        smith_answer["payload"]["headers"].append({"name": gmail.SMITH_HEADER, "value": "1"})
        self.thread = [message("reportmsg01"), message("question01"), message("spoofmsg01", labels=("INBOX",)),
                       message("othermsg01", sender="attacker@example.com"), smith_answer]
        self.sent, self.reads, self.scopes = [], [], f"{gmail.SCOPE} {gmail.READ_SCOPE}"

    def poster(self, url, body, headers):
        return 200, {"access_token": "tok", "scope": self.scopes}

    def reader(self, url, headers):
        self.reads.append(url)
        if "/messages/" in url and "format=full" in url:
            wanted = url.split("/messages/")[1].split("?")[0]
            return 200, next(m for m in self.thread if m["id"] == wanted)
        if "/messages/" in url:
            return 200, {"id": "reportmsg01", "threadId": "thread0001"}
        return 200, {"id": "thread0001", "messages": self.thread}

    def send(self, credentials, **kwargs):
        self.sent.append(kwargs)
        return f"answer-{len(self.sent)}"

    def poll(self, output=None):
        output = output or {"answer": {"text": "비상금은 바로 꺼낼 수 있는 계좌에 두시는 것이 좋습니다.", "refs": []},
                            "follow_up": ["얼마나 모아야 하나요?"]}

        def runner(args, **kwargs):
            envelope = {"subtype": "success", "is_error": False, "structured_output": output, "total_cost_usd": 0.2}
            return subprocess.CompletedProcess(args, 0, json.dumps(envelope), "")

        def build():
            with closing(ledger.connect_read_only(self.db)) as conn:
                return build_report(conn, as_of=AT, known_at=AT, baseline=None, kind="monday")
        return qa.poll(self.db, recipient=ME, credentials=("a", "b", "c"), now=AT, build=build, secrets=[],
                       executable=Path("claude.exe"), runner=runner, sender=self.send, reader=self.reader,
                       poster=self.poster)

    def test_only_the_clients_own_reply_in_smiths_thread_is_answered_once(self):
        results = self.poll()
        self.assertEqual([(r["message_id"], r["status"]) for r in results], [("question01", "answered")])
        reply = self.sent[0]
        self.assertEqual((reply["thread_id"], reply["in_reply_to"]), ("thread0001", "<question01@mail>"))
        self.assertTrue(reply["subject"].startswith("Re: [Smith]"))
        self.assertIn("비상금은 어디에 두는 게 좋을까요?", reply["html"])     # The question, without the quoted report.
        self.assertNotIn("원문", reply["html"])
        # Only Smith's thread is read, by headers; a body is fetched only for the approved question.
        self.assertTrue(all("/threads/thread0001?format=metadata" in u or "/messages/reportmsg01?format=minimal" in u
                            or "/messages/question01?format=full" in u for u in self.reads))
        self.assertEqual(self.poll(), [])                                   # Never answered twice.
        self.assertEqual(len(self.sent), 1)
        with closing(ledger.connect_read_only(self.db)) as conn:
            rows = dict(conn.execute("SELECT message_id, status FROM mail_questions").fetchall())
            uses = [r[0] for r in conn.execute("SELECT use_case FROM advice_runs")]
        # An answer of Smith's whose id was never learned (uncertain send) is not taken for a question.
        self.assertEqual(rows, {"question01": "answered", "spoofmsg01": "skipped", "othermsg01": "skipped", "lostanswer01": "skipped"})
        self.assertNotIn("작성:", reply["html"])                          # Korean quote header removed.
        self.assertEqual(uses, ["mail-answer"])

    def test_doubtful_answers_are_sent_with_a_memo_and_read_access_is_required(self):
        bad = {"answer": {"text": "집값은 반드시 오릅니다. 수익 30%가 보장됩니다.", "refs": []}, "follow_up": []}
        self.poll(bad)
        html = self.sent[0]["html"]
        self.assertIn("반드시 오릅니다", html)                     # Kept: the answer is for the client alone.
        self.assertIn("자동 점검 메모", html)
        self.scopes = gmail.SCOPE                                            # Send-only grant: nothing is read.
        with self.assertRaises(gmail.MailError) as caught:
            self.poll()
        self.assertEqual(caught.exception.code, "no-read-scope")

    def test_ledger_amounts_pass_and_client_assumptions_are_flagged(self):
        self.thread[1] = message("question01", text="IRP가 10억 원이면 어떻게 하나요?")
        output = {"answer": {"text": "현금 1,000만 원은 비상금으로 두십시오. IRP 10억 원이라면 연금 비중이 큽니다.", "refs": []},
                  "follow_up": []}
        self.poll(output)
        html = self.sent[0]["html"]
        self.assertIn("1,000만 원은 비상금으로", html)          # "1,000만 원" matches the ledger's 10,000,000.
        self.assertIn("질문에서 말씀하신 수치(10억 원)", html)   # The client's premise, not a ledger fact.

    def test_clean_failures_are_retried_and_interrupted_answers_recovered(self):
        def failing(credentials, **kwargs):
            raise gmail.MailError("http-400")
        self.send, original = failing, self.send
        self.assertEqual([r["status"] for r in self.poll()], ["failed"])
        self.send = original
        self.assertEqual([r["status"] for r in self.poll()], ["answered"])    # Retried on the next run.
        with closing(ledger.connect(self.db)) as conn:
            ledger.claim_question(conn, message_id="stuck", thread_id="thread0001", report_id=None, received_at=AT, now=AT)
            ledger.start_answer(conn, message_id="stuck", now=AT)
            later = AT.replace(hour=AT.hour + 1)
            self.assertEqual(ledger.recover_questions(conn, now=later)[0]["was"], "sending")
            status = conn.execute("SELECT status FROM mail_questions WHERE message_id = 'stuck'").fetchone()[0]
        self.assertEqual(status, "unknown")                                   # May have been sent: never resent.

    def test_quotes_are_stripped(self):
        self.assertEqual(qa.strip_quote("질문입니다\n> 인용\nOn Mon, Oct 5, 2026 at 6:00 AM Smith wrote:\n원문"), "질문입니다")


if __name__ == "__main__":
    unittest.main()
