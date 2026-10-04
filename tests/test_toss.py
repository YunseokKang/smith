import json
import unittest
from urllib.parse import urlsplit

from smith.toss import ForbiddenRequest, TossClient, TossError
from smith.toss_check import run_check


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


if __name__ == "__main__":
    unittest.main()
