#!/usr/bin/env python3
"""LOCAL_ONLY: loopback CultureFit HTTP slice with a validated public-input flow.

Default start is NETWORK_LOCKED and can never contact an external provider.
Injected HTTPS fixtures exercise the exact client and agent composition.
A real provider backend requires externally verified authorization passed by an
integrating trusted controller; this module never claims that authority itself.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from credential_boundary import make_guarded_client
from lane_identity import LaneIdentityTasteGraph
from place_scope import PlaceScopedTasteGraph
from strict_culture_fit import IncompleteCultureFitFlow, StrictCultureFitAgent
from public_inputs import PublicRequest, parse_public_request
from provider_entity_schema import ArtistSeedTasteGraph
from offline_fixture_transport import OfflineFixtureHTTPSHandler

PUBLIC_SEEDS = ("Taylor Swift",)
PUBLIC_CITY = "Chicago"
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
_HTML = b'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CultureFit | Local evidence bridge</title><style>
body{font-family:system-ui,sans-serif;max-width:48rem;margin:3rem auto;padding:0 1rem;color:#171d2a}
button{padding:.8rem 1.3rem;font-size:1rem}pre{white-space:pre-wrap;overflow-wrap:anywhere}
.notice{background:#f2f5f8;border-left:4px solid #526379;padding:1rem}
</style><h1>CultureFit</h1><p>One fixed cultural-planning flow: Taylor Swift, Chicago.</p>
<p class="notice">Local-only demonstration. Fictional transport responses do not establish
Qloo accuracy, eligibility, permission, or public-release readiness.</p>
<button id="run">Run single bounded flow</button><pre id="result" aria-live="polite"></pre>
<script>document.getElementById('run').addEventListener('click',async()=>{
const b=document.getElementById('run');b.disabled=true;
const r=await fetch('/api/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
const x=await r.json();document.getElementById('result').textContent=JSON.stringify(x,null,2);
});</script></html>'''


class LockedError(RuntimeError):
    pass


class AlreadyUsed(RuntimeError):
    pass


@dataclass
class Backend:
    planner: StrictCultureFitAgent
    mode: str


class Session:
    """One attempt per process, even if the provider returns an error."""

    def __init__(self, backend: Backend | None = None) -> None:
        self.backend = backend
        self._lock = threading.Lock()
        self._used = False

    @property
    def used(self) -> bool:
        with self._lock:
            return self._used

    def plan_once(self, request: PublicRequest | None = None) -> dict:
        if self.backend is None:
            raise LockedError("Provider flow is disabled pending authorization")
        # An authorized frozen live example does not authorize arbitrary user inputs.
        # Dynamic requests are supported ONLY with an injected synthetic transport.
        if request is not None and self.backend.mode != "SYNTHETIC_TRANSPORT_ONLY":
            raise LockedError("Interactive provider requests are not authorized")
        with self._lock:
            if self._used:
                raise AlreadyUsed("This local one-shot flow was already attempted")
            self._used = True
        selection = request if request is not None else PublicRequest(PUBLIC_SEEDS, PUBLIC_CITY)
        outcome = self.backend.planner.plan(seeds=list(selection.seeds), city=selection.city,
                                           take_per_lane=1)
        if request is not None:
            # The Python planner owns Unicode casefold equivalence. Return exact
            # input-to-provider bindings so JS need not approximate casefold().
            resolved = outcome.get("resolved_seeds")
            if not isinstance(resolved, list) or len(resolved) != len(selection.seeds):
                raise IncompleteCultureFitFlow("SEED_BINDINGS_INVALID")
            bindings = []
            for requested_name, entity in zip(selection.seeds, resolved):
                if (not isinstance(entity, dict)
                        or not isinstance(entity.get("name"), str)
                        or entity["name"].casefold() != requested_name.casefold()
                        or not isinstance(entity.get("entity_id"), str)
                        or not entity["entity_id"].strip()):
                    raise IncompleteCultureFitFlow("SEED_BINDINGS_INVALID")
                bindings.append({
                    "requested_name": requested_name,
                    "provider_name": entity["name"],
                    "entity_id": entity["entity_id"],
                })
            outcome = {**outcome, "seed_bindings": bindings}
        if self.backend.mode == "SYNTHETIC_TRANSPORT_ONLY":
            cards = [{**x, "source": "Synthetic Qloo-shaped response; NOT Qloo evidence"}
                     for x in outcome["evidence_cards"]]
            outcome = {**outcome, "evidence_cards": cards,
                       "interpretation_boundary": "Synthetic data only. No real cultural inference."}
        else:
            # Observed provider HTTP, when later authorized, still requires
            # independent validation before any acceptance or publication.
            outcome = {**outcome}
        outcome.update({"execution_mode": self.backend.mode,
                        "provider_independently_verified": False,
                        "complete_required_lanes": True,
                        "public_release_authorized": False,
                        "submission_authorized": False})
        return outcome


def backend_with_guarded_client(api_key: str, *, https_handler=None,
                                allowed_by_external_controller: Callable[[], bool] | None = None,
                                synthetic: bool = False) -> Backend:
    """Construct real composed path; live network requires a separate authority.

    External controller must enforce actual terms, $0 provider authority, claim,
    key origin and exact action. Passing a local boolean is not evidence of it.
    """
    if synthetic and type(https_handler) is not OfflineFixtureHTTPSHandler:
        # Generic HTTPSHandler instances can perform real network requests.
        # Require exact pre-encoded offline fixture transport before any I/O.
        raise LockedError("Synthetic mode requires exact offline fixture transport")
    if not synthetic and (allowed_by_external_controller is None
                          or allowed_by_external_controller() is not True):
        raise LockedError("No externally verified live-execution authority")
    client = make_guarded_client(api_key, https_handler=https_handler)
    graph = PlaceScopedTasteGraph(client)
    if not synthetic:
        # The frozen public example means Taylor Swift the artist. Request that
        # category explicitly; same-name person/album/author results are not
        # silently chosen or conflated with the artist identity.
        graph = ArtistSeedTasteGraph(graph)
    return Backend(StrictCultureFitAgent(LaneIdentityTasteGraph(graph)),
                   "SYNTHETIC_TRANSPORT_ONLY" if synthetic else "LIVE_HTTP_UNREVIEWED")


class Handler(BaseHTTPRequestHandler):
    server_version = "CultureFitGateway/0.1"
    sys_version = ""

    def respond(self, code: int, obj: object, *, html: bool = False):
        try:
            data = _HTML if html else json.dumps(obj, separators=(",", ":"), ensure_ascii=False,
                                                allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            # A malformed result must not drop the HTTP response after an
            # irreversible attempt. Never echo offending provider fields.
            code, html = 502, False
            data = b'{"error":"Bounded response serialization failed"}'
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self.respond(200, {}, html=True)
        elif self.path == "/api/status":
            self.respond(200, {"mode": self.server.session.backend.mode
                              if self.server.session.backend else "NETWORK_LOCKED",
                              "attempted": self.server.session.used,
                              "provider_independently_verified": False})
        else:
            self.respond(404, {"error": "Not found"})

    def do_POST(self):
        if self.path != "/api/plan":
            return self.respond(404, {"error": "Not found"})
        origin = self.headers.get("Origin")
        expected = f"http://127.0.0.1:{self.server.server_port}"
        if origin not in (None, expected):
            return self.respond(403, {"error": "Origin refused"})
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            return self.respond(415, {"error": "JSON only"})
        length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,4}", length) or int(length) > 1024:
            return self.respond(413, {"error": "Body size refused"})
        try:
            data = json.loads(self.rfile.read(int(length)))
            # Legacy {} remains the bounded frozen example; explicit requests
            # must pass the shared parser BEFORE consuming the one-shot fence.
            public_request = None if data == {} else parse_public_request(data)
            output = self.server.session.plan_once(public_request)
        except LockedError:
            return self.respond(503, {"error": "Provider flow remains locked"})
        except AlreadyUsed:
            return self.respond(409, {"error": "One-shot request already attempted"})
        except (IncompleteCultureFitFlow, ValueError) as exc:
            code = getattr(exc, "code", "INCOMPLETE_OR_AMBIGUOUS_FLOW")
            return self.respond(422, {"error": code, "complete_required_lanes": False})
        except Exception:
            # No secrets, HTTP/credential errors, local paths, or returned bodies
            # are exposed in this browser-facing error response.
            return self.respond(502, {"error": "Bounded provider flow failed"})
        return self.respond(200, output)

    def do_OPTIONS(self):
        self.respond(405, {"error": "Not allowed"})

    def log_message(self, *args):
        return  # no headers, API keys, public query, or response contents logged


class Gateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = 0, backend: Backend | None = None):
        self.session = Session(backend)
        super().__init__(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("invalid port")
    with Gateway(port=args.port) as server:
        print(f"CultureFit locked local preview: http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever(poll_interval=.1)


if __name__ == "__main__":
    main()
