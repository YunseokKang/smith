"""Provider credentials in the OS credential store (Windows Credential Manager on Windows).

Credentials never live in repository files, environment variables or logs.
"""
import keyring
from keyring.errors import PasswordDeleteError

_TOSS = "smith.toss"
_GMAIL = "smith.gmail"
_GMAIL_NAMES = ("client_id", "client_secret", "refresh_token")
# Public statistics APIs: read-only data keys, still kept out of files and logs.
EVIDENCE_PROVIDERS = ("ecos", "fred")


def load_toss_client() -> tuple[str, str] | None:
    """Return (client_id, client_secret), or None when either is missing."""
    client_id = keyring.get_password(_TOSS, "client_id")
    client_secret = keyring.get_password(_TOSS, "client_secret")
    if not client_id or not client_secret:
        return None
    return client_id, client_secret


def save_toss_client(client_id: str, client_secret: str) -> None:
    keyring.set_password(_TOSS, "client_id", client_id)
    keyring.set_password(_TOSS, "client_secret", client_secret)


def delete_toss_client() -> None:
    _delete(_TOSS, ("client_id", "client_secret"))


def all_secrets() -> list[str]:
    """Every secret Smith stores, so outbound checks can refuse a model payload that contains one."""
    toss = load_toss_client() or ()
    gmail = load_gmail() or ()
    return [*toss, *gmail, *(load_api_key(p) or "" for p in EVIDENCE_PROVIDERS)]


def load_api_key(provider: str) -> str | None:
    return keyring.get_password(f"smith.{provider}", "api_key") or None


def save_api_key(provider: str, api_key: str) -> None:
    keyring.set_password(f"smith.{provider}", "api_key", api_key)


def delete_api_key(provider: str) -> None:
    _delete(f"smith.{provider}", ("api_key",))


def load_gmail() -> tuple[str, str, str] | None:
    """Return (client_id, client_secret, refresh_token) for the send-only Gmail grant."""
    values = tuple(keyring.get_password(_GMAIL, name) for name in _GMAIL_NAMES)
    return values if all(values) else None  # type: ignore[return-value]


def save_gmail(client_id: str, client_secret: str, refresh_token: str) -> None:
    for name, value in zip(_GMAIL_NAMES, (client_id, client_secret, refresh_token)):
        keyring.set_password(_GMAIL, name, value)


def delete_gmail() -> None:
    _delete(_GMAIL, _GMAIL_NAMES)


def _delete(service: str, names: tuple[str, ...]) -> None:
    for name in names:
        try:
            keyring.delete_password(service, name)
        except PasswordDeleteError:
            pass  # Already absent.
