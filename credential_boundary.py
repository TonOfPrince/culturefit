"""Fail-closed HTTPS-origin and redirect boundary for an existing Qloo client.

This is a composable transport adapter, not an authentication assertion, security
certificate, provider receipt, or license to send a live request. No network is
performed by importing the module or constructing the client.
"""
from __future__ import annotations

from email.message import Message
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

ALLOWED_ORIGIN = "https://hackathon.api.qloo.com"
ALLOWED_ROUTES = frozenset({"/search", "/v2/insights/"})


class CredentialBoundaryError(ValueError):
    """A request would violate the exact approved credential destination."""


def _origin_and_path(url: str) -> tuple[str, str]:
    if not isinstance(url, str) or url != url.strip():
        raise CredentialBoundaryError("invalid URL")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "hackathon.api.qloo.com"
                or parsed.port is not None or parsed.username is not None
                or parsed.password is not None or parsed.fragment):
            raise CredentialBoundaryError("destination is outside approved HTTPS origin")
        if parsed.netloc != "hackathon.api.qloo.com":
            raise CredentialBoundaryError("noncanonical destination authority")
        return parsed.scheme + "://" + parsed.netloc, parsed.path
    except (ValueError, UnicodeError) as exc:
        raise CredentialBoundaryError("invalid destination authority") from exc


def validate_base_url(url: str) -> str:
    """Allow only the documented event host, not arbitrary configured endpoints."""
    origin, path = _origin_and_path(url)
    if path not in ("", "/") or urlsplit(url).query:
        raise CredentialBoundaryError("base URL must have no path or query")
    return origin


class _NoCredentialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib's default redirect handler copies X-Api-Key to new origins.
        # Never construct a second Request bearing this header.
        raise HTTPError(req.full_url, code, "redirect refused", headers, fp)


class GuardedOpener:
    """Check destination before any network I/O and prohibit all HTTP redirects.

    An injected HTTPSHandler is a synthetic-test-only dependency. Production
    construction uses urllib's certificate-verifying standard HTTPSHandler.
    """

    def __init__(self, *, https_handler: HTTPSHandler | None = None) -> None:
        handlers = [ProxyHandler({}), _NoCredentialRedirect()]
        if https_handler is not None:
            handlers.append(https_handler)
        self._opener: OpenerDirector = build_opener(*handlers)

    def __call__(self, req: Request, timeout_seconds: float):
        origin, path = _origin_and_path(req.full_url)
        if origin != ALLOWED_ORIGIN or path not in ALLOWED_ROUTES:
            raise CredentialBoundaryError("request path or origin not authorized")
        if req.get_method() != "GET":
            raise CredentialBoundaryError("only GET requests are authorized")
        if not any(k.lower() == "x-api-key" and str(v).strip()
                   for k, v in req.header_items()):
            raise CredentialBoundaryError("credential header is absent")
        result = self._opener.open(req, timeout=timeout_seconds)
        # A belt-and-suspenders check if an injected handler returns a different URL.
        if hasattr(result, "geturl") and result.geturl() != req.full_url:
            result.close()
            raise CredentialBoundaryError("response origin or URL changed")
        return result


def make_guarded_client(api_key: str, *, base_url: str = ALLOWED_ORIGIN,
                        https_handler: HTTPSHandler | None = None):
    """Compose the existing QlooClient with a strict host and no-redirect transport.

    `https_handler` is only for isolated synthetic tests. No fallback to ambient
    QLOO_BASE_URL or QLOO_API_BASE is permitted for an event credential.
    """
    if not isinstance(api_key, str) or not api_key.strip():
        raise CredentialBoundaryError("explicit event credential required")
    root = validate_base_url(base_url)
    from provider_entity_schema import ProviderTypesQlooClient
    return ProviderTypesQlooClient(api_key=api_key, base_url=root,
                      opener=GuardedOpener(https_handler=https_handler))
