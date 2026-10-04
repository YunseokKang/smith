"""Toss client credentials in the OS credential store (Windows Credential Manager on Windows).

Credentials never live in repository files, environment variables or logs.
"""
import keyring
from keyring.errors import PasswordDeleteError

_SERVICE = "smith.toss"


def load_toss_client() -> tuple[str, str] | None:
    """Return (client_id, client_secret), or None when either is missing."""
    client_id = keyring.get_password(_SERVICE, "client_id")
    client_secret = keyring.get_password(_SERVICE, "client_secret")
    if not client_id or not client_secret:
        return None
    return client_id, client_secret


def save_toss_client(client_id: str, client_secret: str) -> None:
    keyring.set_password(_SERVICE, "client_id", client_id)
    keyring.set_password(_SERVICE, "client_secret", client_secret)


def delete_toss_client() -> None:
    for name in ("client_id", "client_secret"):
        try:
            keyring.delete_password(_SERVICE, name)
        except PasswordDeleteError:
            pass  # Already absent.
