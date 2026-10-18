"""Native read-only provider-status checks over actual loopback servers."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock
from provider_connection import status_via_gateway
from provider_gateway import Gateway, backend_with_guarded_client
from test_provider_closeout import fixture
from web_app import LocalCatalogueServer

class ProviderStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = Path(self.temp.name) / "sources.csv"
        self.external = patch("http.client.HTTPSConnection.connect", side_effect=AssertionError("External HTTPS forbidden")).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.external.assert_not_called)
    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.addCleanup(close)
        return server
    def get(self, server):
        c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            c.request("GET", "/api/provider-status")
            r = c.getresponse()
            return r.status, json.loads(r.read())
        finally:
            c.close()
    def test_unconfigured_status_opens_no_connection(self):
        with patch("provider_connection.http.client.HTTPConnection") as connect:
            self.assertEqual(status_via_gateway(None), (503, {"error": "provider_not_configured"}))
            connect.assert_not_called()
        app = self.serve(LocalCatalogueServer(self.catalog))
        self.assertEqual(self.get(app)[0], 503)
    def test_locked_status_never_consumes_attempt(self):
        gateway = self.serve(Gateway())
        app = self.serve(LocalCatalogueServer(self.catalog, provider_port=gateway.server_port))
        for _ in range(2):
            status, result = self.get(app)
            self.assertEqual(status, 200)
            self.assertEqual(result, {"mode": "NETWORK_LOCKED", "attempted": False, "provider_independently_verified": False})
        self.assertFalse(gateway.session.used)
    def test_synthetic_status_before_and_after_actual_plan(self):
        transport = fixture()
        backend = backend_with_guarded_client("SYNTHETIC-NOT-A-CREDENTIAL", https_handler=transport, synthetic=True)
        gateway = self.serve(Gateway(backend=backend))
        app = self.serve(LocalCatalogueServer(self.catalog, provider_port=gateway.server_port))
        self.assertFalse(self.get(app)[1]["attempted"])
        self.assertEqual(transport.seen_urls, [])
        c = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=5)
        try:
            c.request("POST", "/api/plan", body="{}", headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{app.server_port}"})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            self.assertEqual(len(json.loads(r.read())["evidence_cards"]), 3)
        finally:
            c.close()
        self.assertTrue(self.get(app)[1]["attempted"])
        self.assertEqual(len(transport.seen_urls), 4)
    def test_malformed_status_is_bounded_and_sanitized(self):
        values = [
            {"mode": {}, "attempted": False, "provider_independently_verified": False},
            {"mode": "LIVE_HTTP_UNREVIEWED", "attempted": 1, "provider_independently_verified": False},
            {"mode": "LIVE_HTTP_UNREVIEWED", "attempted": False, "provider_independently_verified": True},
        ]
        for value in values:
            response = MagicMock()
            response.status = 200
            response.getheader.return_value = "application/json"
            response.read.return_value = json.dumps(value).encode()
            connection = MagicMock()
            connection.getresponse.return_value = response
            with patch("provider_connection.http.client.HTTPConnection", return_value=connection):
                self.assertEqual(status_via_gateway(8766), (502, {"error": "provider_status_unavailable"}))
                response.read.assert_called_once_with(4097)
                connection.close.assert_called_once()
    def test_large_status_or_redirect_is_not_followed(self):
        for code, body in [(200, b"x" * 4097), (302, b"{}")]:
            response = MagicMock(status=code)
            response.getheader.return_value = "application/json"
            response.read.return_value = body
            connection = MagicMock()
            connection.getresponse.return_value = response
            with patch("provider_connection.http.client.HTTPConnection", return_value=connection):
                self.assertEqual(status_via_gateway(8766)[0], 502)
                connection.request.assert_called_once_with("GET", "/api/status")
