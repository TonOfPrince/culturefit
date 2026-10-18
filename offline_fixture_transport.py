"""In-memory-only Qloo-shaped HTTPS fixture responder.

This is an explicitly offline test primitive, not a Qloo connection or evidence.
Its ONLY transport operation reads pre-encoded fixture bytes. Never delegates to
urllib's network-opening HTTPSHandler implementation.
"""
from __future__ import annotations

from email.message import Message
from io import BytesIO
import json
from collections.abc import Mapping
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, Request
from urllib.response import addinfourl

_ALLOWED_AUTHORITY = "hackathon.api.qloo.com"
_ALLOWED_PATHS = frozenset(("/search", "/v2/insights/"))


class MissingOfflineFixture(ValueError):
    """Fail closed when no explicitly supplied response matches a request."""


def _allowed_fixture_url(url: object) -> str:
    if not isinstance(url, str):
        raise ValueError("fixture URL must be a string")
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != _ALLOWED_AUTHORITY
            or parts.path not in _ALLOWED_PATHS or not parts.query
            or parts.fragment or parts.username is not None
            or parts.password is not None):
        raise ValueError("fixture URL must target one approved Qloo-shaped GET route")
    return url


class OfflineFixtureHTTPSHandler(HTTPSHandler):
    """Exact-type, immutable, no-network HTTPSHandler for injected tests.

    Construct from {full_https_url: Qloo_shaped_JSON_object}. No callable
    responders, redirects, proxy handlers, socket APIs or network fallback.
    Integrators must check exact type, not merely isinstance(HTTPSHandler),
    before interpreting a synthetic response as proved offline.
    """

    def __init__(self, responses: Mapping[str, dict]) -> None:
        super().__init__()
        if not isinstance(responses, Mapping) or not responses:
            raise ValueError("at least one offline response is required")
        payloads = {}
        for url, payload in responses.items():
            key = _allowed_fixture_url(url)
            if not isinstance(payload, dict):
                raise ValueError("fixture response must be a JSON object")
            payloads[key] = json.dumps(
                payload, ensure_ascii=True, allow_nan=False,
                sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._payloads = payloads
        self.seen_urls: list[str] = []
        self.seen_api_key_present: list[bool] = []

    def https_open(self, req: Request):
        if type(self) is not OfflineFixtureHTTPSHandler:
            raise MissingOfflineFixture("only the exact verified offline fixture type is permitted")
        if not isinstance(req, Request) or req.get_method() != "GET":
            raise MissingOfflineFixture("offline fixture permits only GET")
        key = _allowed_fixture_url(req.full_url)
        self.seen_urls.append(key)
        self.seen_api_key_present.append(any(
            k.lower() == 'x-api-key' and bool(str(v).strip())
            for k, v in req.header_items()))
        body = self._payloads.get(key)
        if body is None:
            raise MissingOfflineFixture("no offline fixture matched request")
        response = addinfourl(BytesIO(body), Message(), key, code=200)
        response.msg = "OK"  # urllib HTTPErrorProcessor expects this field.
        return response
