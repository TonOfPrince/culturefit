"""Ephemeral, key-free hosted demonstration of the existing CultureFit servers.

Every request has a new private temporary directory. Catalogue state is supplied
by the visitor's tab and validated by the existing application. Nothing persists
on the server and no live provider configuration or credential is accepted.
"""
from __future__ import annotations
import http.client
from pathlib import Path
import tempfile
import threading
from curated_catalog import MAX_CATALOG_BYTES
from run_culturefit import demo_backend
from provider_gateway import Gateway
from web_app import LocalCatalogueServer

ROUTES = {
    ("GET", "/api/catalog"),
    ("POST", "/api/catalog"),
    ("POST", "/api/shortlist"),
    ("GET", "/api/provider-status"),
    ("POST", "/api/plan"),
}

def execute(payload):
    fields = {"path", "method", "body", "catalogue_csv", "replace_confirmed"}
    if not isinstance(payload, dict) or set(payload) != fields:
        return 422, {"error": "invalid_demo_request"}
    path, method = payload["path"], payload["method"]
    if (not isinstance(path, str) or not isinstance(method, str) or
            (method, path) not in ROUTES or
            not isinstance(payload["body"], str) or
            type(payload["replace_confirmed"]) is not bool or
            not isinstance(payload["catalogue_csv"], str)):
        return 422, {"error": "invalid_demo_request"}
    body = payload["body"].encode("utf-8")
    catalogue = payload["catalogue_csv"].encode("utf-8")
    if len(body) > MAX_CATALOG_BYTES or len(catalogue) > MAX_CATALOG_BYTES:
        return 413, {"error": "demo_body_too_large"}
    # The hosted demonstration never accepts arbitrary cultural inputs.
    if path == "/api/plan" and body != b"{}":
        return 422, {"error": "frozen_example_only"}
    if method == "GET" and body:
        return 422, {"error": "invalid_demo_request"}
    servers = []
    threads = []
    with tempfile.TemporaryDirectory(prefix="culturefit-demo-") as folder:
        target = Path(folder) / "sources.csv"
        if catalogue:
            target.write_bytes(catalogue)
            target.chmod(0o600)
        try:
            gateway = Gateway(backend=demo_backend())
            app = LocalCatalogueServer(target, provider_port=gateway.server_port)
            servers = [gateway, app]
            for server in servers:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                threads.append(thread)
            connection = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
            try:
                headers = {"Origin": f"http://127.0.0.1:{app.server_port}"}
                if method == "POST":
                    headers["Content-Type"] = "text/csv" if path == "/api/catalog" else "application/json"
                    if payload["replace_confirmed"]:
                        headers["X-Replace-Catalogue"] = "yes"
                connection.request(method, path, body=body if method == "POST" else None, headers=headers)
                response = connection.getresponse()
                raw = response.read(262145)
                if len(raw) > 262144:
                    return 502, {"error": "demo_response_too_large"}
                import json
                result = json.loads(raw)
                return response.status, result
            finally:
                connection.close()
        finally:
            for server in reversed(servers):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=5)
