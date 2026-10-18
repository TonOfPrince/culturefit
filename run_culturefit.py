#!/usr/bin/env python3
"""Start a complete key-free CultureFit demonstration on loopback."""
from __future__ import annotations
import argparse
from pathlib import Path
import tempfile
import threading
from urllib.parse import urlencode
from offline_fixture_transport import OfflineFixtureHTTPSHandler
from provider_gateway import Gateway, backend_with_guarded_client
from web_app import LocalCatalogueServer

def demo_backend():
    def url(path, params):
        return "https://hackathon.api.qloo.com" + path + "?" + urlencode(params)
    def response(items):
        return {"success": True, "results": {"entities": items}}
    seed = {"entity_id": "fictional-seed", "name": "Taylor Swift", "subtype": "urn:entity:artist"}
    replies = {url("/search", [("query", "Taylor Swift"), ("take", "5")]): response([seed])}
    for family, label in [("place", "Fictional Chicago venue"), ("brand", "Fictional brand partner"), ("artist", "Fictional collaborating artist")]:
        params = [("filter.type", "urn:entity:" + family), ("signal.interests.entities", "fictional-seed"), ("take", "1"), ("signal.location.query", "Chicago")]
        if family == "place":
            params += [("filter.location.query", "Chicago"), ("filter.location.radius", "0")]
        replies[url("/v2/insights/", params)] = response([{"entity_id": "fictional-" + family, "name": label, "subtype": "urn:entity:" + family}])
    return backend_with_guarded_client("SYNTHETIC-NOT-A-CREDENTIAL", https_handler=OfflineFixtureHTTPSHandler(replies), synthetic=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true", help="Fictional responses; never contacts Qloo")
    mode.add_argument("--gateway-port", type=int, help="Separately configured trusted loopback gateway")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--catalog", type=Path, help="Optional operator-owned catalogue path")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535 or (args.gateway_port is not None and not 1 <= args.gateway_port <= 65535):
        parser.error("invalid port")
    gateway = None
    temp = tempfile.TemporaryDirectory(prefix="culturefit-")
    try:
        provider_port = args.gateway_port
        if args.offline:
            gateway = Gateway(backend=demo_backend())
            thread = threading.Thread(target=gateway.serve_forever, daemon=True)
            thread.start()
            provider_port = gateway.server_port
        with LocalCatalogueServer(args.catalog or Path(temp.name) / "sources.csv", port=args.port, provider_port=provider_port) as app:
            print(f"CultureFit: http://127.0.0.1:{app.server_port} — " + ("OFFLINE fictional demonstration" if args.offline else "configured gateway; no new execution authority"), flush=True)
            try:
                app.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        if gateway:
            gateway.shutdown()
            gateway.server_close()
            thread.join(timeout=5)
        temp.cleanup()

if __name__ == "__main__":
    main()
