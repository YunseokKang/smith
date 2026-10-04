import argparse
import hashlib
import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from smith import advise_command, adviser, cases, ledger
from smith.importer import parse_import
from smith.payload import MAX_PAYLOAD_BYTES, PayloadRejected, build_context, check_outbound, load_view, redact

AT = "2026-10-01T00:00:00+00:00"
NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def rec(record_id: str, kind: str, owner: str = "self", **fields: object) -> dict:
    return {"id": record_id, "kind": kind, "owner_id": owner, "effective_at": AT, "revision": 1,
            "status": "active", **fields}


def asset(record_id: str, value: str, liquidity: str, category: str = "cash", account: str = "bank",
          owner: str = "self", **extra: object) -> dict:
    return rec(record_id, "asset", owner, category=category, account_type=account, currency="KRW", value=value,
               valuation_method="manual", liquidity=liquidity, **extra)


RECORDS = [
    asset("cash-mybank-main", "50000000", "immediate"),
    asset("stock-kr-1", "80000000", "days", "stock", "brokerage", symbol="005930", market="KR",
          quantity="1000", unit_price="80000", average_cost="60000"),
    asset("pension-1", "40000000", "restricted", "unclassified", "pension_savings"),
    asset("kis-1", "10000000", "days", "unclassified", "brokerage", managed_by="hermes"),
    asset("home-1", "1000000000", "months", "real_estate", "none", occupancy="owner_occupied"),
    asset("home-2", "500000000", "months", "real_estate", "none", owner="partner-private",
          occupancy="leased_out"),
    rec("loan-1", "liability", category="mortgage", currency="KRW", outstanding_principal="200000000",
        annual_rate="0.04", rate_type="variable", repayment_method="equal_principal", collateral_record_id="home-1",
        reason="note with 010-1234-5678"),  # Free text in the ledger must never reach the payload.
    rec("deposit-2", "liability", "partner-private", category="lease_deposit_obligation", currency="KRW",
        outstanding_principal="300000000", annual_rate="0", rate_type="fixed", repayment_method="bullet",
        maturity="2028-08-07", collateral_record_id="home-2"),
    rec("salary", "cashflow", direction="inflow", category="salary", currency="KRW", amount="9000000",
        frequency="monthly", start_date="2026-01-01"),
    rec("repay", "cashflow", direction="outflow", category="loan_payment", currency="KRW", amount="1500000",
        frequency="monthly", start_date="2026-01-01", liability_record_id="loan-1", commitment="fixed"),
    rec("prepay", "cashflow", direction="outflow", category="loan_payment", currency="KRW", amount="500000",
        frequency="monthly", start_date="2026-01-01", liability_record_id="loan-1", commitment="discretionary"),
]
VALID_ADVICE = {
    "recommendation": {"summary": "s", "rationale": "r"},
    "personal_basis": [{"refs": ["A1", "L1", "case"], "point": "p"}],
    "external_basis": [{"refs": ["ecos:722Y001-0101000"], "point": "p"}],
    "alternatives": [{"name": n, "description": "d", "cost": "c", "risk": "r", "liquidity": "l", "pros": [], "cons": []}
                     for n in ("a", "b")],
    "reconsider_if": ["x"], "data_limitations": ["y"],
}


class AdviserTests(unittest.TestCase):
    def setUp(self):
        self.view = self.make_view(RECORDS, ("self", "partner-private"))

    def make_view(self, records, owners=("self",)):
        conn = ledger.connect(":memory:")
        self.addCleanup(conn.close)
        doc = {"schema_version": 1, "import_id": "m1", "source": "manual", "mode": "patch", "as_of": AT,
               "owners": [{"id": owner} for owner in owners], "records": records}
        ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=NOW)
        return load_view(conn, as_of=NOW, known_at=NOW)

    def test_payload_is_aliased_minimal_and_checked(self):
        # Positions (symbols) are sent only for the portfolio case, which needs them.
        context = build_context(self.view, {}, include_positions=False)
        text = json.dumps(context, ensure_ascii=False)
        for leaked in ("cash-mybank-main", "loan-1", "home-1", "partner-private", "note with", "005930"):
            self.assertNotIn(leaked, text)
        self.assertIn("household_member_1", text)
        self.assertIn("005930", json.dumps(build_context(self.view, {}, include_positions=True)))
        sanitized_question = redact(self.view, "cash-mybank-main과 partner-private 자산은?")
        self.assertNotIn("cash-mybank-main", sanitized_question)
        self.assertNotIn("partner-private", sanitized_question)
        check_outbound(text, secrets=["s3cret-key-value"])  # Dates and amounts are not identifiers.
        cases_ = [("account-number", "110-123-456789"), ("resident-registration-number", "900101-5234567"),
                  ("email", "me@example.com"), ("phone", "010-1234-5678"), ("card-number", "1234-5678-9012-3456"),
                  ("labelled-name", "이름: 홍길동"), ("korean-address", "서울특별시 강남구 테헤란로 123"),
                  ("english-address", "123 Main Street"), ("stored-secret", "s3cret-key-value")]
        for rule, sample in cases_:
            with self.subTest(rule), self.assertRaises(PayloadRejected) as caught:
                check_outbound(f"question about {sample}", secrets=["s3cret-key-value"])
            self.assertIn(rule, caught.exception.rules)
        with self.assertRaises(PayloadRejected) as caught:
            check_outbound("x" * (MAX_PAYLOAD_BYTES + 1), secrets=[])
        self.assertIn("payload-too-large", caught.exception.rules)

    def test_case_facts_are_computed_in_code(self):
        funding = cases.funding_facts(self.view, amount=Decimal(100_000_000))
        self.assertEqual(funding["reachable_by_liquidity"]["immediate"], {"cumulative": "50000000", "covers_target": False})
        self.assertEqual(funding["reachable_by_liquidity"]["days"], {"cumulative": "140000000", "covers_target": True})
        tiers = [(t["tier"], t["cumulative"], t["covers_target"]) for t in funding["funding_tiers"]]
        self.assertEqual(tiers, [("cash", "50000000", False), ("kr_listed_stocks", "130000000", True),
                                 ("hermes_managed", "140000000", True), ("restricted", "180000000", True),
                                 ("illiquid", "1180000000", True)])  # Hermes assets are tapped last but one.
        self.assertEqual(funding["pausable_monthly_outflow"], "500000")
        self.assertEqual(cases.portfolio_facts(self.view)["monthly_discretionary_outflow"], "500000")
        self.assertEqual(funding["restricted_total_self"], "40000000")
        gains = {s["ref"]: s["unrealized_gain"] for s in funding["sources"]}
        self.assertEqual(gains[self.view.aliases["stock-kr-1"]], "20000000")
        home = cases.home_facts(self.view, target_price=Decimal(2_000_000_000), target_date=date(2029, 10, 1))
        base = home["scenarios"][0]
        self.assertEqual((home["months"], home["partner_property_equity"]), (36, "200000000"))
        self.assertEqual(base["required_with_costs"], str((Decimal(2_000_000_000) * Decimal("1.02") ** 3
                                                           * Decimal("1.035")).quantize(Decimal(1))))

    def test_unknown_amounts_are_not_treated_as_zero(self):
        records = [
            asset("cash", "50000000", "immediate"),
            rec("usd-stock", "asset", category="stock", account_type="brokerage", currency="USD", value="100000",
                valuation_method="market", liquidity="days", symbol="XYZ", market="US", quantity="100",
                unit_price="1000", average_cost="800"),
        ]
        view = self.make_view(records)
        portfolio = cases.portfolio_facts(view)
        funding = cases.funding_facts(view, amount=Decimal(100_000_000))
        home = cases.home_facts(view, target_price=Decimal(2_000_000_000), target_date=date(2029, 10, 4))
        unknown_ref = view.aliases["usd-stock"]
        self.assertIsNone(portfolio["securities_total"])
        self.assertIn(unknown_ref, portfolio["unknown_amount_refs"])
        self.assertIsNone(funding["reachable_by_liquidity"]["days"]["cumulative"])
        self.assertIsNone(funding["us_stock_tax_estimate_if_all_sold"])
        self.assertIsNone(home["scenarios"][0]["available_self"])

    def test_funding_coverage_uses_after_tax_us_stock_amount(self):
        records = [
            asset("us-stock", "100000000", "days", "stock", "brokerage", symbol="XYZ", market="US",
                  quantity="100", unit_price="1000000", average_cost="0"),
        ]
        view = self.make_view(records)
        tier = cases.funding_facts(view, amount=Decimal(90_000_000))["funding_tiers"][0]
        self.assertEqual(tier["estimated_tax"], "21450000")
        self.assertEqual(tier["available_after_estimated_tax"], "78550000")
        self.assertEqual((tier["covers_target"], tier["shortfall_after"]), (False, "11450000"))

    def test_home_uses_active_self_cash_flow_and_only_mortgage_interest(self):
        records = [
            asset("home", "1000000000", "months", "real_estate", "none", occupancy="owner_occupied"),
            asset("cash", "100000000", "immediate"),
            rec("mortgage", "liability", category="mortgage", currency="KRW", outstanding_principal="200000000",
                annual_rate="0.04", rate_type="fixed", repayment_method="equal_principal",
                collateral_record_id="home"),
            rec("other-loan", "liability", category="credit_loan", currency="KRW",
                outstanding_principal="120000000", annual_rate="0.12", rate_type="fixed",
                repayment_method="equal_principal"),
            rec("salary", "cashflow", direction="inflow", category="salary", currency="KRW", amount="9000000",
                frequency="monthly", start_date="2026-01-01"),
            rec("mortgage-pay", "cashflow", direction="outflow", category="loan_payment", currency="KRW",
                amount="1500000", frequency="monthly", start_date="2026-01-01", liability_record_id="mortgage"),
            rec("other-pay", "cashflow", direction="outflow", category="loan_payment", currency="KRW",
                amount="2000000", frequency="monthly", start_date="2026-01-01", liability_record_id="other-loan"),
            rec("future-income", "cashflow", direction="inflow", category="salary", currency="KRW",
                amount="100000000", frequency="monthly", start_date="2027-01-01"),
            rec("ended-cut", "cashflow", direction="outflow", category="living_expense", currency="KRW",
                amount="7000000",
                frequency="monthly", start_date="2026-01-01", end_date="2026-09-01", commitment="discretionary"),
            rec("partner-income", "cashflow", "partner-private", direction="inflow", category="salary", currency="KRW",
                amount="100000000", frequency="monthly", start_date="2026-01-01"),
        ]
        view = self.make_view(records, ("self", "partner-private"))
        facts = cases.home_facts(view, target_price=Decimal(2_000_000_000), target_date=date(2029, 10, 4))
        self.assertEqual(facts["estimated_monthly_principal_repayment"], "833333")
        self.assertEqual(facts["monthly_net_cash_flow_self"], "5500000")
        self.assertEqual(facts["projected_savings_from_monthly_net"], "198000000")
        self.assertEqual(cases.portfolio_facts(view)["monthly_discretionary_outflow"], "0")

    def test_headless_boundary_and_output_validation(self):
        context = build_context(self.view, {}, include_positions=False)
        calls = []

        def runner(advice=VALID_ADVICE, **envelope):
            def run(args, **kwargs):
                calls.append((args, kwargs))
                out = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.1,
                       "structured_output": advice, **envelope}
                return subprocess.CompletedProcess(args, 0, json.dumps(out), "")
            return run

        with mock.patch.dict(os.environ, {"SMITH_TEST_SECRET": "x"}):
            serialized = json.dumps({"question": "QUESTION-MARKER", "context": context}, ensure_ascii=False, indent=1)
            result = adviser.run_adviser("QUESTION-MARKER", context, executable=Path("claude.exe"), prompt=serialized,
                                         runner=runner())
        args, kwargs = calls[0]
        self.assertEqual(result["advice"], VALID_ADVICE)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--safe-mode", args)
        self.assertIn("--no-session-persistence", args)
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertEqual(kwargs["env"][adviser.HEADLESS_ENV], "1")
        self.assertNotIn("SMITH_TEST_SECRET", kwargs["env"])
        self.assertNotEqual(Path(kwargs["cwd"]).resolve(), Path.cwd().resolve())
        self.assertNotIn("QUESTION-MARKER", " ".join(args))  # The question travels on stdin only.
        self.assertEqual(kwargs["input"], serialized)
        bad = {**VALID_ADVICE, "alternatives": VALID_ADVICE["alternatives"][:1]}
        made_up = {**VALID_ADVICE, "personal_basis": [{"refs": ["A99"], "point": "p"}]}
        made_up_in_prose = json.loads(json.dumps(VALID_ADVICE))
        made_up_in_prose["recommendation"]["summary"] = "A99를 매도"
        for label, run, code in [("schema", runner(bad), "schema-violation"),
                                 ("unknown ref", runner(made_up), "unknown-refs"),
                                 ("unknown ref in prose", runner(made_up_in_prose), "unknown-refs"),
                                 ("model error", runner(is_error=True), "model-error")]:
            with self.subTest(label), self.assertRaises(adviser.AdviserError) as caught:
                adviser.run_adviser("q", context, executable=Path("claude.exe"), runner=run)
            self.assertEqual(caught.exception.code, code)

        def timeout(args, **kwargs):
            raise subprocess.TimeoutExpired(args, 1)
        with self.assertRaises(adviser.AdviserError) as caught:
            adviser.run_adviser("q", context, executable=Path("claude.exe"), runner=timeout)
        self.assertEqual(caught.exception.code, "timeout")
        with mock.patch.dict(os.environ, {adviser.HEADLESS_ENV: "1"}), self.assertRaises(adviser.AdviserError):
            adviser.run_adviser("q", context, executable=Path("claude.exe"), runner=runner())
        with self.assertRaises(adviser.AdviserError) as caught:
            adviser.run_adviser("q", context, executable=Path("claude.exe"), prompt='{"question":"other"}',
                                runner=runner())
        self.assertEqual(caught.exception.code, "input-mismatch")

    def test_advice_audit_record_hashes_the_exact_payload(self):
        conn = ledger.connect(":memory:")
        self.addCleanup(conn.close)
        payload = '{"question":"q","context":{"amount":"100"}}'
        ledger.record_advice_run(conn, run_id="run-1", created_at=NOW, use_case="funding", question="q",
                                 payload=payload, prompt_version=adviser.PROMPT_VERSION, model="opus", cost_usd="0.4",
                                 outcome="success", advice='{"ok":true}')
        row = conn.execute("SELECT payload, payload_sha256, outcome FROM advice_runs WHERE run_id = 'run-1'").fetchone()
        self.assertEqual(row, (payload, hashlib.sha256(payload.encode("utf-8")).hexdigest(), "success"))

    def test_successful_advice_is_withheld_when_audit_write_fails(self):
        args = argparse.Namespace(budget_usd="1.00", model="opus", case="portfolio", db=Path("unused.db"))
        result = {"advice": VALID_ADVICE, "cost_usd": "0.1", "prompt_version": adviser.PROMPT_VERSION}
        out = io.StringIO()
        with mock.patch.object(adviser, "find_claude", return_value=Path("claude.exe")), \
                mock.patch.object(adviser, "run_adviser", return_value=result), \
                mock.patch.object(advise_command, "_log", return_value=False), redirect_stdout(out):
            code = advise_command._ask(args, self.view, "q", {}, '{"question":"q","context":{}}', NOW)
        self.assertEqual(code, 1)
        self.assertIn("withheld", out.getvalue())
        self.assertNotIn("[추천]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
