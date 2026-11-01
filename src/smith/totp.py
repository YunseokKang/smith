"""Time-based one-time passwords (RFC 6238: HMAC-SHA1, 6 digits, 30-second steps) for trade approval.

A trade is approved only by a code from the client's authenticator app (trading design §5). The secret lives in the
OS credential store and nothing Smith sends or receives by mail contains it, so an app or agent that can read and
send the client's Gmail still cannot approve a trade.
"""
import base64
import hashlib
import hmac
import re
import secrets
import struct
import urllib.parse
from datetime import datetime

STEP_SECONDS = 30
DIGITS = 6
# One step either side of the message time absorbs phone clock drift and the seconds spent typing.
WINDOW = 1
_CODE = re.compile(r"\d{6}")


def new_secret() -> str:
    """A fresh 160-bit secret, base32 without padding (what authenticator apps accept for manual entry)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def step_of(moment: datetime) -> int:
    return int(moment.timestamp()) // STEP_SECONDS


def code_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 10 ** DIGITS:0{DIGITS}d}"


def matching_step(secret: str, code: str, at: datetime) -> int | None:
    """The step within WINDOW of `at` whose code is `code`, or None. The caller rejects a step already used."""
    if not _CODE.fullmatch(code):
        return None
    center = step_of(at)
    for step in range(center - WINDOW, center + WINDOW + 1):
        if hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, *, label: str = "Smith trade approval") -> str:
    """The otpauth:// URI an authenticator app imports (shown once at enrolment, never stored or logged)."""
    name = urllib.parse.quote(label)
    return f"otpauth://totp/{name}?secret={secret}&issuer=Smith&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
