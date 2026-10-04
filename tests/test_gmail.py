import base64
import email
import hashlib
import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from email import policy
from pathlib import Path

from smith import gmail
from smith.config import load_config

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "smith.example.toml"


def fake_browser(state_override=None, error=None):
    """Plays Google: reads the consent URL, then calls the loopback redirect like a browser would."""
    seen = {}

    def open_browser(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        seen.update(query)
        params = {"state": state_override or query["state"]}
        params.update({"error": error} if error else {"code": "one-time-code"})
        target = f"{query['redirect_uri']}/?{urllib.parse.urlencode(params)}"
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()
    return open_browser, seen


class GmailTests(unittest.TestCase):
    def test_login_uses_pkce_state_and_send_only_scope(self):
        exchanged = {}

        def poster(url, body, headers):
            exchanged.update(dict(urllib.parse.parse_qsl(body.decode())))
            return 200, {"refresh_token": "r-token", "scope": gmail.SCOPE}

        browser, seen = fake_browser()
        self.assertEqual(gmail.login("cid", "secret", open_browser=browser, poster=poster), "r-token")
        self.assertEqual((seen["scope"], seen["code_challenge_method"], seen["access_type"]),
                         (gmail.SCOPE, "S256", "offline"))
        self.assertTrue(seen["redirect_uri"].startswith("http://127.0.0.1:"))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(exchanged["code_verifier"].encode()).digest()).rstrip(b"=")
        self.assertEqual(challenge.decode(), seen["code_challenge"])
        self.assertEqual(exchanged["code"], "one-time-code")
        for label, browser_args, code in [("forged redirect", {"state_override": "attacker"}, "state-mismatch"),
                                          ("denied", {"error": "access_denied"}, "consent-denied")]:
            with self.subTest(label), self.assertRaises(gmail.MailError) as caught:
                gmail.login("cid", "secret", open_browser=fake_browser(**browser_args)[0], poster=poster)
            self.assertEqual(caught.exception.code, code)

    def test_send_builds_the_message_and_flags_uncertain_outcomes(self):
        sent = {}

        def poster(url, body, headers, send_status=200):
            if url == gmail.TOKEN_URL:
                return 200, {"access_token": "a-token"}
            sent["raw"] = json.loads(body)["raw"]
            sent["auth"] = headers["Authorization"]
            return send_status, {"id": "msg-1"} if send_status == 200 else {}

        message_id = gmail.send(("cid", "secret", "r-token"), recipient="me@example.com", subject="[Smith] 보고",
                                html="<p>본문</p>", text="본문", poster=poster)
        message = email.message_from_bytes(base64.urlsafe_b64decode(sent["raw"]), policy=policy.default)
        self.assertEqual((message_id, message["To"], str(message["Subject"]), sent["auth"]),
                         ("msg-1", "me@example.com", "[Smith] 보고", "Bearer a-token"))
        self.assertEqual(message.get_body(("html",)).get_content().strip(), "<p>본문</p>")

        def dropped(url, body, headers):
            if url == gmail.TOKEN_URL:
                return 200, {"access_token": "a-token"}
            raise TimeoutError()
        def accepted_without_id(url, body, headers):
            return (200, {"access_token": "a-token"}) if url == gmail.TOKEN_URL else (200, {})
        cases = [("lost response", dropped, True), ("server error", lambda u, b, h: poster(u, b, h, 503), True),
                 ("200 with a lost body", accepted_without_id, True),
                 ("rejected", lambda u, b, h: poster(u, b, h, 400), False)]
        for label, fake, uncertain in cases:
            with self.subTest(label), self.assertRaises(gmail.MailError) as caught:
                gmail.send(("cid", "secret", "r-token"), recipient="me@example.com", subject="s", html="h", text="t",
                           poster=fake)
            self.assertEqual(caught.exception.uncertain, uncertain)  # Uncertain sends are never retried blindly.

    def test_recipient_must_be_a_single_address(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.toml"
            for value, ok in [("me@example.com", True), ("a@x.com, b@y.com", False), ("not-an-email", False)]:
                with self.subTest(value):
                    path.write_text(EXAMPLE.read_text(encoding="utf-8") + f'\n[mail]\nrecipient = "{value}"\n',
                                    encoding="utf-8")
                    if ok:
                        self.assertEqual(load_config(path)["mail"]["recipient"], value)
                    else:
                        self.assertRaises(ValueError, load_config, path)


if __name__ == "__main__":
    unittest.main()
