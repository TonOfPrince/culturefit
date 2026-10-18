"""Bounded loopback connection to an explicitly configured CultureFit gateway.

The separate gateway owns credentials, transport, authorization and one-shot
state. No gateway is selected by default. This connection grants no live access.
"""
from __future__ import annotations

import http.client
import json

MAX_PLAN_BYTES = 1024
MAX_RESPONSE_BYTES = 262144
GATEWAY_TIMEOUT_SECONDS = 90.0
MODES = {"SYNTHETIC_TRANSPORT_ONLY", "LIVE_HTTP_UNREVIEWED"}


def validate_port(port: int | None) -> None:
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("Provider port must be an integer from 1 to 65535")


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _decode(data: bytes) -> dict:
    value = json.loads(
        data.decode("utf-8"), object_pairs_hook=_unique_fields,
        parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def plan_via_gateway(port: int | None, body: bytes) -> tuple[int, dict]:
    """Forward once to one fixed loopback route; never retry an uncertain attempt."""
    validate_port(port)
    if port is None:
        return 503, {"error": "provider_not_configured"}
    if not 0 < len(body) <= MAX_PLAN_BYTES:
        return 413, {"error": "body_too_large"}
    try:
        payload = _decode(body)
        if payload and set(payload) != {"seeds", "city"}:
            raise ValueError("unexpected fields")
    except (ValueError, UnicodeError):
        return 422, {"error": "invalid_provider_request"}

    # HTTPConnection does not consult ambient proxies or follow redirects.
    # The operator must configure the trusted local gateway; no URL, hostname,
    # path, credential or authorization flag is accepted from a web request.
    connection = http.client.HTTPConnection("127.0.0.1", port,
                                            timeout=GATEWAY_TIMEOUT_SECONDS)
    try:
        connection.request("POST", "/api/plan", body=body,
                           headers={"Content-Type": "application/json",
                                    "Origin": f"http://127.0.0.1:{port}"})
        response = connection.getresponse()
        if response.getheader("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise ValueError("unexpected gateway content type")
        data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("gateway response too large")
        result = _decode(data)
        if response.status == 200:
            # Preserve the original gateway result and its honest provenance.
            # A local response is neither authenticated Qloo evidence nor review.
            if (result.get("product") != "CultureFit"
                    or not isinstance(result.get("execution_mode"), str)
                    or result["execution_mode"] not in MODES
                    or result.get("provider_independently_verified") is not False
                    or result.get("public_release_authorized") is not False
                    or result.get("submission_authorized") is not False
                    or result.get("complete_required_lanes") is not True):
                raise ValueError("unexpected gateway result")
            return 200, result
        if response.status == 409:
            return 409, {"error": "provider_already_attempted"}
        if response.status == 503:
            return 503, {"error": "provider_flow_locked"}
        if response.status == 422:
            allowed = {"SEED_AMBIGUOUS", "SEED_NOT_RESOLVED", "REQUIRED_LANE_EMPTY",
                       "INVALID_PUBLIC_INPUT", "EMAIL_OR_URL_NOT_ALLOWED",
                       "DUPLICATE_ANCHOR", "INCOMPLETE_OR_AMBIGUOUS_FLOW"}
            error = result.get("error")
            return 422, {"error": error if isinstance(error, str) and error in allowed else "invalid_provider_request",
                         "complete_required_lanes": False}
    except (OSError, http.client.HTTPException, ValueError, UnicodeError):
        pass
    finally:
        connection.close()
    # The gateway may have consumed its attempt before a disconnect or timeout.
    # No automatic repeat, lock reset or claim of an unused attempt is safe.
    return 502, {"error": "provider_connection_failed",
                 "attempt_state": "unknown", "retry_automatically": False}


def status_via_gateway(port: int | None) -> tuple[int, dict]:
    """Read the trusted gateway state without consuming its provider attempt."""
    validate_port(port)
    if port is None:
        return 503, {"error": "provider_not_configured"}
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/api/status")
        response = connection.getresponse()
        if (response.status != 200 or
                response.getheader("Content-Type", "").split(";", 1)[0].strip() != "application/json"):
            raise ValueError("unexpected gateway status")
        data = response.read(4097)
        if len(data) > 4096:
            raise ValueError("status response too large")
        value = _decode(data)
        if (not isinstance(value.get("mode"), str)
                or value["mode"] not in MODES | {"NETWORK_LOCKED"}
                or type(value.get("attempted")) is not bool
                or value.get("provider_independently_verified") is not False):
            raise ValueError("invalid gateway state")
        return 200, {"mode": value["mode"], "attempted": value["attempted"],
                     "provider_independently_verified": False}
    except (OSError, http.client.HTTPException, ValueError, UnicodeError):
        return 502, {"error": "provider_status_unavailable"}
    finally:
        connection.close()
