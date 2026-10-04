"""Shared HTTP opener that never follows redirects.

Following a redirect would forward broker tokens (Authorization header) or API keys carried in
the URL to whatever host the redirect names, and could let another site's content pass as an
official source. A 3xx response is therefore surfaced as an HTTPError for the caller to reject.
"""
import urllib.request
from typing import Any


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None  # urllib then raises HTTPError with the 3xx status.


_OPENER = urllib.request.build_opener(_NoRedirect)


def open_url(request: urllib.request.Request | str, *, timeout: float) -> Any:
    """Open a URL without following redirects. Raises urllib errors like `urlopen`."""
    return _OPENER.open(request, timeout=timeout)
