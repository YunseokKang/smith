"""Gmail delivery through the Gmail API with the send-only scope.

Verified against Google's docs on 2026-10-04: desktop OAuth uses the loopback redirect with PKCE
(https://developers.google.com/identity/protocols/oauth2/native-app) and sending is
`POST gmail/v1/users/me/messages/send` with a base64url RFC 2822 message and the
`gmail.send` scope. Smith never requests read access to the mailbox.

A request that may have reached Google but returned no answer has an unknown outcome; it is
reported as such and never retried automatically, so a report is not sent twice.
"""
import base64
import hashlib
import http.client
import http.server
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from smith.net import open_url

SCOPE = "https://www.googleapis.com/auth/gmail.send"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_TIMEOUT_SECONDS = 30
_LOGIN_TIMEOUT_SECONDS = 300

# (url, form-or-json body, headers) -> (status, json payload)
Poster = Callable[[str, bytes, dict[str, str]], tuple[int, dict[str, Any]]]


class MailError(Exception):
    """A mail operation failed. `uncertain` means the message may have been sent anyway."""

    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        super().__init__(code)
        self.code, self.uncertain = code, uncertain


def load_client_file(path: Path) -> tuple[str, str]:
    """Read client_id and client_secret from a Google 'Desktop app' client JSON file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        installed = data["installed"]
        return str(installed["client_id"]), str(installed["client_secret"])
    except (OSError, ValueError, KeyError, TypeError):
        raise MailError("invalid-client-file") from None


def post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with open_url(request, timeout=_TIMEOUT_SECONDS) as response:
            return response.status, _json(response.read())
    except urllib.error.HTTPError as error:
        with error:
            return error.code, _json(error.read())


def _json(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def login(client_id: str, client_secret: str, *, open_browser: Callable[[str], Any] = webbrowser.open,
          poster: Poster = post) -> str:
    """Run the browser consent flow on a loopback port and return a refresh token.

    Raises:
        MailError: consent was denied or timed out, the state did not match, or no refresh token came back.
    """
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    received: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            if "code" in query or "error" in query:
                received.update(query)
            body = "Smith: 인증 절차가 끝났습니다. 이 창을 닫으셔도 됩니다.".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:  # The query holds the one-time code; never log it.
            pass

    with http.server.HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 1
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
        open_browser(AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
            "access_type": "offline", "prompt": "consent"}))
        deadline = time.monotonic() + _LOGIN_TIMEOUT_SECONDS
        while not received and time.monotonic() < deadline:
            server.handle_request()
    if not received:
        raise MailError("consent-timeout")
    if not secrets.compare_digest(received.get("state", ""), state):
        raise MailError("state-mismatch")  # Possible forged redirect; the code is not used.
    if "error" in received:
        raise MailError("consent-denied")
    status, payload = poster(TOKEN_URL, urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret, "code": received["code"],
        "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect_uri}).encode("ascii"),
        {"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200 or not payload.get("refresh_token"):
        raise MailError("token-exchange-failed" if status != 200 else "no-refresh-token")
    if SCOPE not in str(payload.get("scope", "")).split():
        raise MailError("send-scope-not-granted")
    return str(payload["refresh_token"])


def send(credentials: tuple[str, str, str], *, recipient: str, subject: str, html: str, text: str,
         poster: Poster = post) -> str:
    """Send one message to the configured recipient and return Gmail's message id.

    Raises:
        MailError: with `uncertain=True` when the send request may have been accepted.
    """
    client_id, client_secret, refresh_token = credentials
    try:
        status, payload = poster(TOKEN_URL, urllib.parse.urlencode({
            "client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token,
            "grant_type": "refresh_token"}).encode("ascii"), {"Content-Type": "application/x-www-form-urlencoded"})
    except (OSError, http.client.HTTPException):
        raise MailError("network-error") from None  # Nothing was sent yet.
    if status != 200 or not payload.get("access_token"):
        raise MailError("token-refresh-failed" if status != 400 else "login-required")
    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    try:
        status, payload = poster(SEND_URL, json.dumps({"raw": raw}).encode("utf-8"),
                                 {"Authorization": f"Bearer {payload['access_token']}",
                                  "Content-Type": "application/json"})
    except (OSError, http.client.HTTPException):
        raise MailError("unknown-outcome", uncertain=True) from None
    if status == 200 and payload.get("id"):
        return str(payload["id"])
    # A success status whose body (the sent Message) is lost or malformed, or a server error, may still
    # have delivered the message, so neither is a clean failure that could be retried.
    accepted = 200 <= status < 300
    raise MailError(f"http-{status}-no-id" if accepted else f"http-{status}", uncertain=accepted or status >= 500)
