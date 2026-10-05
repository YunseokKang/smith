import json
import unittest
import urllib.error
from unittest import mock
from datetime import datetime, timezone
from urllib.parse import urlsplit

from smith import ledger
from smith.records import Action, ImportRejected, Status
from smith.toss import ForbiddenRequest, TossClient, TossError, urllib_transport
from smith.toss_check import run_check
from smith.toss_sync import SyncError, collect_snapshot


class FakeTransport:
    """Serves queued responses per path and records every request that reached the 'network'."""

    def __init__(self, responses: dict[str, list[tuple[int, dict]]]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
        path = urlsplit(url).path
        self.requests.append((method, path, headers))
        status, payload = self.responses[path].pop(0)
        return status, json.dumps(payload).encode("utf-8")

    def paths(self) -> list[str]:
        return [path for _, path, _ in self.requests]


TOKEN = (200, {"access_token": "t", "token_type": "Bearer", "expires_in": 86400})
ACCOUNTS = (200, {"result": [{"accountNo": "123-45-678901", "accountSeq": 7, "accountType": "BROKERAGE"}]})
HOLDINGS = (200, {"result": {"marketValue": {"amount": {"krw": "720000", "usd": None}},
                             "items": [{"symbol": "005930", "name": "Sample", "currency": "KRW",
                                        "quantity": "10", "marketValue": {"amount": "720000"}}]}})
POWER = (200, {"result": {"currency": "KRW", "cashBuyingPower": "50000"}})
RATE = (200, {"result": {"midRate": "1400.5", "validFrom": "2026-10-04T09:00:00+09:00"}})


class TossTests(unittest.TestCase):
    def test_requests_outside_the_read_only_allowlist_never_reach_the_network(self):
        transport = FakeTransport({})
        client = TossClient("id", "secret", transport=transport)
        for method, path in [("POST", "/api/v1/orders"), ("POST", "/api/v1/orders/1/cancel"),
                             ("DELETE", "/api/v1/conditional-orders/1"), ("GET", "/api/v1/orders")]:
            with self.subTest(path=path), self.assertRaises(ForbiddenRequest):
                client._send(method, path, None, {})
        self.assertEqual(transport.requests, [])

    def test_check_hides_values_and_reissues_an_expired_token_once(self):
        expired = (401, {"error": {"code": "expired-token", "requestId": "r1", "message": "secret detail"}})
        transport = FakeTransport({
            "/oauth2/token": [TOKEN, TOKEN],
            "/api/v1/accounts": [expired, ACCOUNTS],
            "/api/v1/holdings": [HOLDINGS],
            "/api/v1/buying-power": [POWER, POWER],
            "/api/v1/exchange-rate": [RATE],
        })
        lines: list[str] = []
        code = run_check(TossClient("id", "secret", transport=transport), show_values=False, emit=lines.append)
        output = "\n".join(lines)
        self.assertEqual(code, 0)
        self.assertEqual(transport.paths().count("/oauth2/token"), 2)
        self.assertTrue(all(h.get("X-Tossinvest-Account") == "7" for _, p, h in transport.requests
                            if p in ("/api/v1/holdings", "/api/v1/buying-power")))
        for hidden in ("123-45-678901", "720000", "005930", "50000", "1400.5"):
            self.assertNotIn(hidden, output)

    def test_network_and_shape_failures_become_safe_codes(self):
        with mock.patch("smith.toss.open_url", side_effect=urllib.error.URLError("dns fail for host.example")):
            with self.assertRaises(TossError) as caught:
                urllib_transport("GET", "https://openapi.tossinvest.com/api/v1/accounts", {}, None)
        self.assertEqual((caught.exception.code, str(caught.exception)), ("network-error", "HTTP 0 network-error"))
        no_result = FakeTransport({"/oauth2/token": [TOKEN], "/api/v1/accounts": [(200, {"data": []})]})
        with self.assertRaises(TossError) as caught:
            TossClient("id", "secret", transport=no_result).accounts()
        self.assertEqual(caught.exception.code, "invalid-response")

    def test_failure_reports_only_code_request_id_and_hint(self):
        forbidden = (403, {"error": {"code": "forbidden-ip", "requestId": "r9", "message": "ip 1.2.3.4"}})
        transport = FakeTransport({"/oauth2/token": [forbidden]})
        lines: list[str] = []
        code = run_check(TossClient("id", "secret", transport=transport), show_values=False, emit=lines.append)
        output = "\n".join(lines)
        self.assertEqual(code, 1)
        self.assertIn("forbidden-ip", output)
        self.assertIn("allowed IPs", output)
        self.assertNotIn("1.2.3.4", output)
        with self.assertRaises(TossError) as caught:
            TossClient("id", "secret", transport=FakeTransport({"/oauth2/token": [(401, {"error": "invalid_client"})]})).accounts()
        self.assertEqual(caught.exception.code, "invalid_client")


def holding(symbol: str, amount: str, currency: str = "KRW") -> dict:
    return {"symbol": symbol, "name": f"Sample {symbol}", "marketCountry": "KR", "currency": currency,
            "quantity": "10", "lastPrice": "100", "averagePurchasePrice": "90",
            "marketValue": {"amount": amount, "amountAfterCost": amount}}


def sync_transport(*items: dict, holdings: dict | None = None, rate: tuple = RATE) -> FakeTransport:
    return FakeTransport({
        "/oauth2/token": [TOKEN], "/api/v1/accounts": [ACCOUNTS],
        "/api/v1/holdings": [(200, {"result": {"items": list(items)} if holdings is None else holdings})],
        "/api/v1/buying-power": [POWER, POWER], "/api/v1/exchange-rate": [rate],
    })


class TossSyncTests(unittest.TestCase):
    def setUp(self):
        self.conn = ledger.connect(":memory:")
        self.addCleanup(self.conn.close)

    def sync(self, day: int, *items: dict, close_missing: bool = False, **transport: object):
        at = datetime(2026, 10, day, tzinfo=timezone.utc)
        batch = collect_snapshot(TossClient("id", "secret", transport=sync_transport(*items, **transport)),
                                 owner_id="self", collected_at=at)
        result = ledger.apply_import(self.conn, batch, recorded_at=at, close_missing=close_missing)
        return [action for _, _, action in result.actions]

    def test_snapshot_lifecycle(self):
        self.assertEqual(self.sync(5, holding("AAA", "1000"), holding("BBB", "2000")), [Action.CREATED] * 2)
        self.assertEqual(self.sync(6, holding("AAA", "1000"), holding("BBB", "2000")), [Action.UNCHANGED] * 2)
        with self.assertRaises(ImportRejected) as caught:
            self.sync(7, holding("AAA", "1100"))
        self.assertIn("missing from this snapshot", caught.exception.problems[0])
        self.assertEqual(self.sync(8, holding("AAA", "1100"), close_missing=True), [Action.UPDATED, Action.CLOSED])
        self.assertEqual(self.sync(9, holding("AAA", "1100"), holding("BBB", "2100")),
                         [Action.UNCHANGED, Action.UPDATED])  # A position bought back reopens.
        now = datetime(2026, 10, 10, tzinfo=timezone.utc)
        states = ledger.record_states(self.conn, as_of=now, known_at=now)
        self.assertEqual(sorted(s.record.fields["value"] for s in states), ["1100", "2100"])
        self.assertNotIn("123-45-678901", json.dumps([s.record.record_id for s in states]))
        metrics = {key[1] for key in ledger.latest_observations(self.conn, as_of=now, known_at=now)}
        self.assertEqual(metrics, {"cash_buying_power", "fx_mid_rate"})  # Buying power is not an asset.

    def test_malformed_responses_abort_before_anything_is_stored(self):
        self.sync(5, holding("AAA", "1000"))
        no_quantity = {k: v for k, v in holding("BBB", "10").items() if k != "quantity"}
        rate = lambda **r: (200, {"result": {"midRate": "1400", "validFrom": "2026-10-04T09:00:00+09:00", **r}})
        cases = [
            ("items missing", {"holdings": {}}),  # Must not read as "everything was sold".
            ("items not a list", {"holdings": {"items": "x"}}),
            ("duplicate symbol", {"holdings": {"items": [holding("AAA", "1"), holding("AAA", "2")]}}),
            ("field missing", {"holdings": {"items": [no_quantity]}}),
            ("zero fx", {"rate": rate(midRate="0")}),
            ("naive fx time", {"rate": rate(validFrom="2026-10-04T09:00:00")}),
        ]
        for label, transport in cases:
            with self.subTest(label), self.assertRaises(SyncError):
                self.sync(6, close_missing=True, **transport)
        now = datetime(2026, 10, 7, tzinfo=timezone.utc)
        states = ledger.record_states(self.conn, as_of=now, known_at=now)
        self.assertEqual([s.record.status for s in states], [Status.ACTIVE])

    def test_the_scheduled_sync_closes_sold_positions_but_never_all_at_once(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path

        from smith import cli
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db = Path(directory.name) / "smith.db"

        def run(*items: dict, extra: tuple = ()) -> int:
            batch = collect_snapshot(TossClient("id", "secret", transport=sync_transport(*items)), owner_id="self",
                                     collected_at=datetime.now(timezone.utc))
            with mock.patch("smith.credentials.load_toss_client", return_value=("id", "secret")), \
                    mock.patch("smith.toss_sync.collect_snapshot", return_value=batch), redirect_stdout(io.StringIO()):
                return cli.main(["toss", "sync", "--close-missing", "--db", str(db), *extra])
        self.assertEqual(run(holding("AAA", "1000"), holding("BBB", "2000")), 0)
        self.assertEqual(run(holding("AAA", "1000")), 0)              # BBB was sold: closed, sync not blocked.
        self.assertEqual(run(), 1)                                     # No holdings at all: refused.
        self.assertEqual(run(extra=("--allow-empty",)), 0)             # Confirmed by hand.

    def test_unsupported_currency_aborts_the_whole_sync(self):
        with self.assertRaises(SyncError):
            self.sync(5, holding("AAA", "1000"), holding("HKX", "10", currency="HKD"))
        self.assertEqual(ledger.record_states(self.conn, as_of=datetime(2027, 1, 1, tzinfo=timezone.utc),
                                              known_at=datetime(2027, 1, 1, tzinfo=timezone.utc)), [])


if __name__ == "__main__":
    unittest.main()
