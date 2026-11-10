"""The spouse's replies to their own letter (report design §42.11): only an authenticated reply from the configured
address is a question, the answer goes to that address, sees the household from the spouse's side with the letter's
own narrative, and keeps nothing. Synthetic household and mailbox; nothing is sent."""
import base64
import json
import subprocess
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from smith import gmail, ledger, memory, narrative, partner_letter, partner_qa, qa, qa_lookup, research
from smith.importer import parse_import

import fast_ledger

AT = datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc)   # Friday 06:00 in Seoul.
ME, SPOUSE = "me@example.com", "family@example.org"
HOUSEHOLD = {"partner_birth_year": 1990, "birth_year": 1990}
CONFIG = {"app": {"timezone": "Asia/Seoul"}, "reports": {}, "mail": {"recipient": ME, "partner_recipient": SPOUSE}}
GMAIL_PASS = ("mx.google.com; dkim=pass header.i=@example.org header.s=s1 header.b=abc; spf=pass smtp.mailfrom=x; "
              "dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=example.org")
NO_LOOKUP = {"needed": "no", "holding": "", "topic": "none", "subject": ""}


def b64(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def rec(record_id, kind, owner, **fields):
    return {"id": record_id, "kind": kind, "owner_id": owner, "effective_at": "2026-10-01T00:00:00+00:00",
            "revision": 1, "status": "active", **fields}


def stock(record_id, owner, symbol, market, quantity, price):
    return rec(record_id, "asset", owner, category="stock", account_type="brokerage", currency="KRW",
               value=str(quantity * price), valuation_method="market", liquidity="days", symbol=symbol, market=market,
               quantity=str(quantity), unit_price=str(price), average_cost=str(price), instrument_name=symbol)


def mail(message_id, thread_id, *, sender, labels, auth=(), text="제 IRP는 더 넣는 게 좋을까요?"):
    headers = [{"name": "Authentication-Results", "value": value} for value in auth]
    headers += [{"name": "From", "value": f"<{sender}>"}, {"name": "Message-ID", "value": f"<{message_id}@mail>"}]
    return {"id": message_id, "threadId": thread_id, "labelIds": list(labels), "internalDate": str(int((AT + timedelta(
            minutes=30)).timestamp() * 1000)), "payload": {"mimeType": "text/plain", "headers": headers,
                                                          "body": {"data": b64(text)}}}


class PartnerQuestionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "smith.db"
        records = [rec("cash", "asset", "self", category="cash", account_type="bank", currency="KRW", value="11000000",
                       valuation_method="manual", liquidity="immediate"),
                   rec("cash-partner", "asset", "partner", category="cash", account_type="bank", currency="KRW",
                       value="37000000", valuation_method="manual", liquidity="immediate"),
                   # Both hold the same share in different amounts; only the client holds MSFT.
                   stock("sec-self", "self", "005930", "KR", 10, 60000), stock("msft-self", "self", "MSFT", "US", 5, 600000),
                   stock("sec-partner", "partner", "005930", "KR", 20, 60000)]
        doc = {"schema_version": 1, "import_id": "p1", "source": "manual", "mode": "patch",
               "as_of": "2026-10-01T00:00:00+00:00", "owners": [{"id": "self"}, {"id": "partner"}], "records": records}
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
            # The client's report of the day, and the narratives of both letters (each audited under its own use case).
            ledger.claim_report(conn, report_id="r1", slot=None, kind="tax", trigger="manual", created_at=AT, as_of=AT,
                                baseline=None, missed_slots=[])
            ledger.finish_report(conn, report_id="r1", status="sent", finished_at=AT, subject="[Smith] 11/07 금요 세금·연금",
                                 message_id="clientletter01")
            for run_id, use_case, said in (("cfinal", narrative.FINAL_USE_CASE, "고객님 레터의 판단"),
                                           ("pfinal", narrative.FINAL_USE_CASE + narrative.PARTNER_USE_CASE, "배우자 레터의 판단")):
                ledger.record_advice_run(conn, run_id=run_id, created_at=AT, use_case=use_case, question="tax", payload="{}",
                                         prompt_version="v", model="m", cost_usd=None, outcome="success",
                                         advice=json.dumps({"verified": {"situation": said}}))
        sent = partner_letter.publish(self.db, CONFIG, now=AT, kind="tax", trigger="scheduled", credentials=("a", "b", "c"),
                                      household=HOUSEHOLD, sender=lambda credentials, **kwargs: "spouseletter01",
                                      snapshot_at=AT)
        self.key = sent["run_key"]
        with closing(ledger.connect(self.db)) as conn:
            ledger.record_daily_snapshot(conn, run_key=self.key, day=AT.date(), created_at=AT, snapshot={
                "product": partner_letter.PRODUCT, "snapshot_at": AT.isoformat(), "baseline": None,
                "narrative_run_ids": ["pfinal"]})
        # The client's own memory: never in the spouse's call.
        memory.add(memory.directory(self.db), "plan", "고객님만 아는 이직 계획이 있다.", on=AT.date(), source="대화")
        self.threads = {"threadclient": [mail("clientletter01", "threadclient", sender=ME, labels=("SENT",))],
                        "threadspouse": [
                            mail("spouseletter01", "threadspouse", sender=ME, labels=("SENT",)),
                            mail("spousereply01", "threadspouse", sender=SPOUSE, labels=("INBOX",), auth=(GMAIL_PASS,)),
                            mail("forgedreply01", "threadspouse", sender=SPOUSE, labels=("INBOX",)),
                            # A forged verdict below a failing one of Gmail's own does not count.
                            mail("forgedreply02", "threadspouse", sender=SPOUSE, labels=("INBOX",),
                                 auth=("mx.google.com; dmarc=fail header.from=example.org", GMAIL_PASS)),
                            mail("clientnote01", "threadspouse", sender=ME, labels=("SENT",)),
                            mail("strangerreply1", "threadspouse", sender="other@example.net", labels=("INBOX",),
                                 auth=("mx.google.com; dmarc=pass header.from=example.net",))]}
        self.sent, self.reads = [], []

    def reader(self, url, headers):
        self.reads.append(url)
        if "/threads/" in url:
            thread_id = url.split("/threads/")[1].split("?")[0]
            return 200, {"id": thread_id, "messages": self.threads[thread_id]}
        wanted = url.split("/messages/")[1].split("?")[0]
        found = next(m for messages in self.threads.values() for m in messages if m["id"] == wanted)
        return 200, found if "format=full" in url else {"id": wanted, "threadId": found["threadId"]}

    def send(self, credentials, **kwargs):
        self.sent.append(kwargs)
        return f"answer{len(self.sent):04d}"

    def poll(self, output=None, partner_recipient=SPOUSE, later=None):
        """`later`: the model's output from the second call on (a look-up's second pass)."""
        base = {"answer": {"text": "IRP는 연말까지 남은 한도를 보고 정하시면 됩니다.", "refs": []}, "follow_up": [],
                "remember": [], "lookup": NO_LOOKUP}
        outputs = [{**base, **(output or {})}] + ([{**base, **later}] if later else [])

        def answering(args, **kwargs):
            reply = outputs.pop(0) if len(outputs) > 1 else outputs[0]
            body = {"subtype": "success", "is_error": False, "structured_output": reply, "total_cost_usd": 0.1}
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        now = AT + timedelta(hours=1)
        answerer = partner_qa.answerer(self.db, now=now, tz="Asia/Seoul", secrets=[], household=HOUSEHOLD, lineage=None,
                                       executable=Path("claude.exe"), runner=answering)
        return qa.poll(self.db, recipient=ME, credentials=("a", "b", "c"), now=now, secrets=[], tz="Asia/Seoul",
                       build=lambda report: qa.rebuild(self.db, report, now=now, household=HOUSEHOLD),
                       executable=Path("claude.exe"), runner=answering, sender=self.send, reader=self.reader,
                       poster=lambda url, body, headers: (200, {"access_token": "tok",
                                                                "scope": f"{gmail.SCOPE} {gmail.READ_SCOPE}"}),
                       partner=answerer, partner_recipient=partner_recipient)

    def test_only_the_spouses_authenticated_reply_is_answered_and_to_the_spouse(self):
        results = self.poll()
        self.assertEqual([(r["message_id"], r["status"]) for r in results], [("spousereply01", "answered")])
        reply, = self.sent
        self.assertEqual((reply["recipient"], reply["thread_id"], reply["in_reply_to"]),
                         (SPOUSE, "threadspouse", "<spousereply01@mail>"))
        with closing(ledger.connect_read_only(self.db)) as conn:
            rows = {m: (s, c) for m, s, c in conn.execute("SELECT message_id, status, error_code FROM mail_questions")}
            product = conn.execute("SELECT product FROM mail_questions WHERE message_id = 'spousereply01'").fetchone()[0]
            uses = [r[0] for r in conn.execute("SELECT use_case FROM advice_runs WHERE use_case LIKE 'mail-%'")]
            payload = json.loads(conn.execute("SELECT payload FROM advice_runs WHERE use_case LIKE 'mail-%'").fetchone()[0])
        self.assertEqual(product, "partner")
        self.assertEqual(rows["forgedreply01"], ("skipped", "partner-unauthenticated"))
        self.assertEqual(rows["forgedreply02"], ("skipped", "partner-unauthenticated"))
        self.assertEqual(rows["clientnote01"], ("skipped", "not-from-partner"))     # The client's own note is no question.
        self.assertEqual(rows["strangerreply1"], ("skipped", "not-from-partner"))
        self.assertEqual(uses, ["mail-answer" + narrative.PARTNER_USE_CASE])
        # Her side of the household, her letter's narrative, no client memory.
        self.assertEqual(payload["asker"], "spouse")
        self.assertEqual(payload["client_memory"], [])
        self.assertNotIn("이직 계획", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["recent_report"]["situation"], "배우자 레터의 판단")
        # Her own holdings, as in her letter; the household's come apart with whose they are (Codex review 2026-11-10).
        self.assertEqual({(h["ticker"], h["quantity"]) for h in payload["holdings"]}, {("005930", "20")})
        held = {h["ticker"]: h["held_by"] for h in payload["household_portfolio_facts"]["largest"]}
        self.assertEqual(held, {"005930": "both", "MSFT": "spouse"})               # From her side, MSFT is the spouse's.
        # The client's own accounts never reach her answer (Codex review 2026-11-10); hers do.
        owners = {a["owner"] for a in payload["household"]["assets"]}
        self.assertEqual(owners, {"self"})
        self.assertNotIn("11000000", json.dumps(payload["household"]["assets"]))
        self.assertIn("37000000", json.dumps(payload["household"]["assets"]))
        self.assertEqual(payload["report_notes"], [])            # The system notes carry the client's account readings.
        # She looks up only her own holdings: the client's MSFT is not offered.
        self.assertNotIn("MSFT", {c["symbol"] for c in payload["lookup_candidates"]["holdings"]})
        self.assertIn("005930", {c["symbol"] for c in payload["lookup_candidates"]["holdings"]})
        self.assertEqual(self.poll(), [])                                           # Never answered twice.

    def test_a_dividend_on_its_record_date_is_counted_from_the_spouses_side(self):
        record = partner_qa.letter(self.db, self.key)
        data = partner_qa.rebuild(self.db, record, tz="Asia/Seoul", household=HOUSEHOLD)
        declared = {"symbol": "005930", "currency": "KRW", "per_share": Decimal(100), "kind": "분기배당",
                    "record_date": date(2026, 10, 15), "pay_date": None, "ref": "R9", "checked": True}
        found = qa_lookup.estimates(self.db, data, [declared], known_at=AT)
        self.assertEqual({(r["owner"], r["quantity"], r["quantity_basis"]) for r in found["items"]},
                         {("self", "20", "record_date"), ("household_member_1", "10", "record_date")})
        # Her answer counts her own lots only, on the record date and on the report date alike.
        for record_date in (date(2026, 10, 15), None):
            mine = qa_lookup.estimates(self.db, data, [{**declared, "record_date": record_date}], known_at=AT,
                                       owners=frozenset({"self"}))
            self.assertEqual([(r["owner"], r["quantity"]) for r in mine["items"]], [("self", "20")])
            self.assertEqual(mine["total"]["net"], next(r["net"] for r in mine["items"]))

    def test_her_brief_keeps_public_topics_and_her_own_companies_only(self):
        data = partner_qa.rebuild(self.db, partner_qa.letter(self.db, self.key), tz="Asia/Seoul", household=HOUSEHOLD)
        refs = research.holding_refs(data["view"])
        keys = {(p["symbol"], p["market"]): p["key"] for p in data["holdings"]["positions"]}
        hers, his = refs[keys[("005930", "KR")]], refs[keys[("MSFT", "US")]]
        brief = {"brief_id": "b", "brief": {"gaps": [f"MSFT ({his}): no reliable guidance found"],
                                            "items": [{"ref": "R1", "topic": hers}, {"ref": "R2", "topic": his},
                                                      {"ref": "R3", "topic": "T1"}]}}
        kept = qa.spouse_brief(brief, data)
        self.assertEqual([i["ref"] for i in kept["brief"]["items"]], ["R1", "R3"])       # Not the client's MSFT.
        self.assertEqual(kept["brief"]["gaps"], [])                    # Free text that may name his holdings (Codex).
        self.assertIsNone(qa.spouse_brief(None, data))

    def test_a_dividend_look_up_in_her_answer_carries_her_lots_only(self):
        # Codex review 2026-11-10 (P0): the look-up's second input carried the client's lots, accounts and amounts.
        declared = {"symbol": "005930", "currency": "KRW", "per_share": Decimal(100), "kind": "분기배당",
                    "record_date": date(2026, 10, 15), "pay_date": None, "ref": "R1", "checked": True}
        found = {"items": [], "dividends": [declared], "unproven_dividends": [], "gaps": [], "status": "done",
                 "reason": ""}
        first = {"answer": {"text": "확인해 보겠습니다.", "refs": []},
                 "lookup": {"needed": "yes", "holding": "005930", "topic": "dividend", "subject": ""}}
        with mock.patch.object(qa_lookup, "gate", return_value=(SimpleNamespace(topic="dividend", subject="005930"), None)), \
                mock.patch.object(qa_lookup, "look_up", return_value=found):
            self.poll(first, later={"answer": {"text": "배당은 기준일 보유분으로 계산됩니다.", "refs": []}})
        with closing(ledger.connect_read_only(self.db)) as conn:
            second = json.loads(conn.execute("SELECT payload FROM advice_runs WHERE use_case = ?",
                                             ("mail-answer-looked-up" + narrative.PARTNER_USE_CASE,)).fetchone()[0])
        self.assertEqual([(r["owner"], r["quantity"]) for r in second["dividend_estimates"]["items"]], [("self", "20")])
        self.assertEqual({a["owner"] for a in second["household"]["assets"]}, {"self"})

    def test_nothing_the_spouse_states_is_kept_and_no_reply_says_so(self):
        stated = [{"topic": "plan", "fact": "배우자는 내년에 IRP를 해지할 계획이다."}]
        promise = "말씀하신 계획은 다음 보고서부터 반영하겠습니다. IRP 한도는 연말까지 보시면 됩니다."
        self.poll({"answer": {"text": promise, "refs": []}, "remember": stated})
        html = self.sent[0]["html"]
        self.assertNotIn("기억해 두", html)
        self.assertIn("배우자분께 전해 주시면", html)                     # The one true sentence for the spouse.
        self.assertIn(qa.SPOUSE_PROMISE_MEMO, html)
        self.assertEqual([f.text for f in memory.load(memory.directory(self.db))], ["고객님만 아는 이직 계획이 있다."])

    def test_a_promise_to_keep_what_the_spouse_said_is_replaced(self):
        # Codex review 2026-11-10: "기억해 두겠습니다" alone (no "다음 보고서부터") passed in the spouse's reply.
        self.poll({"answer": {"text": "말씀하신 IRP 해지 계획은 기억해 두겠습니다. IRP 한도는 연말까지 보시면 됩니다.",
                              "refs": []}})
        html = self.sent[0]["html"]
        self.assertNotIn("기억해 두", html)
        self.assertIn(qa.SPOUSE_SAFE_SENTENCE, html)
        self.assertIn("IRP 한도는 연말까지 보시면 됩니다.", html)
        self.assertFalse(qa.keeps_fact(qa.SPOUSE_SAFE_SENTENCE) or qa.promises_change(qa.SPOUSE_SAFE_SENTENCE))

    def test_the_spouses_threads_are_not_read_without_the_configured_address(self):
        self.assertEqual(self.poll(partner_recipient=None), [])
        self.assertFalse(any("threadspouse" in url or "spouseletter01" in url for url in self.reads))
        self.assertEqual(self.sent, [])


class SpousePayloadTests(unittest.TestCase):
    def test_the_other_members_own_records_and_sales_are_left_out(self):
        # Codex review 2026-11-10: the client's account balances and this year's sales reached the spouse's input.
        payload = {"household": {"assets": [
                       {"owner": "self", "category": "cash", "value": "1"},
                       {"owner": "household_member_1", "category": "deposit", "value": "2"},
                       {"owner": "household_member_1", "category": "real_estate", "value": "3"},
                       {"owner": "household_member_1", "category": "lease_deposit", "value": "4"}],
                       "liabilities": [{"owner": "household_member_1", "outstanding": "5"}],
                       "cash_flow": {"monthly_net": "6", "items": [{"owner": "self", "amount": "7"},
                                                                  {"owner": "household_member_1", "amount": "8"}]}},
                   "tax": {"notes": [], "realized_this_year": {"sales": [
                       {"owner": "self", "symbol": "VOO"}, {"owner": "household_member_1", "symbol": "MSFT"}],
                       "self_us_realized_gain_won": "9"}}}
        seen = qa.spouse_payload(payload)
        self.assertEqual([a["value"] for a in seen["household"]["assets"]], ["1", "3", "4"])   # Hers, the homes, deposits.
        self.assertEqual(seen["household"]["liabilities"], payload["household"]["liabilities"])  # Loans: in her letter.
        self.assertEqual([i["amount"] for i in seen["household"]["cash_flow"]["items"]], ["7"])
        self.assertEqual(seen["household"]["cash_flow"]["monthly_net"], "6")                  # Household totals stay.
        self.assertEqual([s["symbol"] for s in seen["tax"]["realized_this_year"]["sales"]], ["VOO"])
        self.assertEqual(seen["tax"]["realized_this_year"]["self_us_realized_gain_won"], "9")


class AuthenticatedTests(unittest.TestCase):
    def verdict(self, *values, domain="example.org"):
        return qa.authenticated([{"name": "Authentication-Results", "value": v} for v in values], domain)

    def test_gmails_dmarc_or_dkim_verdict_for_the_senders_domain(self):
        self.assertTrue(self.verdict("mx.google.com; dmarc=pass (p=NONE) header.from=example.org"))
        self.assertTrue(self.verdict("mx.google.com; dkim=pass header.i=@mail.example.org header.s=a; dmarc=none"))
        self.assertFalse(self.verdict("mx.google.com; dkim=pass header.i=@evil.example; spf=pass"))   # Another signer.
        self.assertFalse(self.verdict("mx.google.com; spf=pass smtp.mailfrom=example.org"))           # SPF alone.
        self.assertFalse(self.verdict("relay.evil.example; dmarc=pass header.from=example.org"))      # Not Gmail's.
        self.assertFalse(self.verdict("mx.google.com; dmarc=pass header.from=example.org.evil.example"))
        self.assertFalse(self.verdict("mx.google.com; dkim=pass header.d=notexample.org"))
        self.assertFalse(self.verdict())
        self.assertFalse(self.verdict("mx.google.com; dmarc=pass header.from=example.org", domain=""))
        # Codex review 2026-11-10: the domain is the whole signer after the last "@", and comments are not verdicts.
        self.assertFalse(self.verdict("mx.google.com; dkim=pass header.i=example.org@attacker.example; "
                                      "dmarc=fail header.from=example.org"))
        self.assertFalse(self.verdict("mx.google.com; dkim=fail (dmarc=pass header.from=example.org) header.i=@x.example"))
        self.assertFalse(self.verdict("mx.google.com; dmarc=pass header.from=mail.example.org"))      # DMARC is exact.
        self.assertTrue(self.verdict("mx.google.com; dkim=pass header.d=example.org header.s=a"))


def setUpModule():  # noqa: N802 - unittest hook
    fast_ledger.install()


def tearDownModule():  # noqa: N802 - unittest hook
    fast_ledger.uninstall()


if __name__ == "__main__":
    unittest.main()
