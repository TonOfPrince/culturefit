"""Native integration contract for the standalone local catalogue API.

Only 127.0.0.1 sockets and operator-authored local CSV records are exercised.
The separately held browser JavaScript is not part of these tests.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import web_app  # noqa: E402
from web_app import LocalCatalogueServer  # noqa: E402

DATA = (b"lane,name,city,tags,source_url,source_label\n"
        b"place,Sample Venue,Minneapolis,jazz;coffee,https://example.org/venue,Operator notes\n"
        b"brand,Sample Brand,,coffee,https://example.org/brand,Operator notes\n"
        b"artist,Sample Artist,,jazz,https://example.org/artist,Artist website\n")


class NativeWebApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = Path(self.temp.name) / "catalog.csv"
        self.server = LocalCatalogueServer(self.catalog, 0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    def request(self, method, path, data=None, extra=None):
        headers = {"Host": f"127.0.0.1:{self.port}",
                   "Origin": f"http://127.0.0.1:{self.port}"}
        if extra:
            headers.update(extra)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        status, raw, response_headers = response.status, response.read(), dict(response.getheaders())
        conn.close()
        return status, json.loads(raw) if path.startswith('/api/') else raw, response_headers

    def upload(self, data=DATA, replace=False):
        return self.request("POST", "/api/catalog", data, {
            "Content-Type": "text/csv", "X-Replace-Catalogue": "yes" if replace else "no"})

    def search(self, city="Minneapolis", tags="jazz,coffee"):
        return self.request("POST", "/api/shortlist", json.dumps({
            "city": city, "interests": tags, "per_lane": 3}).encode(),
            {"Content-Type": "application/json"})

    def test_native_import_and_three_typed_provenance_lanes(self):
        code, imported, _ = self.upload()
        self.assertEqual((code, imported["record_count"]), (201, 3))
        self.assertEqual(self.catalog.read_bytes(), DATA)
        self.assertEqual(self.catalog.stat().st_mode & 0o777, 0o600)
        code, found, headers = self.search()
        self.assertEqual(code, 200)
        self.assertEqual([len(found["lanes"][lane]) for lane in ("place", "brand", "artist")], [1, 1, 1])
        self.assertEqual(found["provider_calls"], 0)
        self.assertEqual(found["lanes"]["place"][0]["source_label"], "Operator notes")
        self.assertEqual(found["source_status"], "user_supplied_unverified")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_missing_records_remain_missing_and_city_bounds_places(self):
        self.upload()
        self.assertEqual(self.search(city="Chicago")[1]["missing_lanes"], ["place"])
        empty = self.search(tags="electronic")[1]
        self.assertEqual(empty["missing_lanes"], ["place", "brand", "artist"])
        self.assertFalse(any(empty["lanes"].values()))

    def test_invalid_import_never_overwrites_existing_catalogue(self):
        self.upload()
        before = self.catalog.read_bytes()
        self.assertEqual(self.upload(b"invalid,data\n", replace=True)[0], 422)
        self.assertEqual(self.upload(DATA)[0], 409)
        self.assertEqual(self.catalog.read_bytes(), before)
        self.assertEqual(list(self.catalog.parent.glob(".culturefit-*.csv")), [])

    def test_origin_and_host_denials_precede_mutation(self):
        self.assertEqual(self.upload(DATA, replace=False)[0], 201)
        before = self.catalog.read_bytes()
        self.assertEqual(self.request("POST", "/api/catalog", DATA, {
            "Content-Type": "text/csv", "Origin": "https://bad.example",
            "X-Replace-Catalogue": "yes"})[0], 403)
        self.assertEqual(self.request("GET", "/api/catalog", extra={"Host": "bad.example"})[0], 403)
        self.assertEqual(self.catalog.read_bytes(), before)

    def test_invalid_search_and_content_type_fail_closed(self):
        self.upload()
        self.assertEqual(self.request("POST", "/api/shortlist", b'NaN',
                                      {"Content-Type": "application/json"})[0], 422)
        self.assertEqual(self.request("POST", "/api/shortlist", b'{}',
                                      {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/shortlist", b'{"city":"M","interests":"a","provider_mode":"live"}',
                                      {"Content-Type": "application/json"})[0], 422)

    def test_shortlist_duplicate_json_fields_rejected_without_catalogue_change(self):
        self.assertEqual(self.upload()[0], 201)
        before = self.catalog.read_bytes()
        ambiguous = [
            b'{"city":"Minneapolis","city":"Chicago","interests":"jazz"}',
            b'{"city":"Minneapolis","interests":"jazz","interests":"coffee"}',
            b'{"city":"Minneapolis","interests":"jazz","per_lane":1,"per_lane":3}',
            b'{"city":"Minneapolis","interests":"jazz","required_tags":"jazz","required_tags":"coffee"}',
        ]
        for body in ambiguous:
            with self.subTest(body=body):
                code, result, _headers = self.request(
                    "POST", "/api/shortlist", body,
                    {"Content-Type": "application/json"})
                self.assertEqual((code, result), (422, {"error": "invalid_search"}))
        self.assertEqual(self.catalog.read_bytes(), before)
        self.assertEqual(self.search()[0], 200)

    def test_unpaired_unicode_surrogates_return_422_and_preserve_server(self):
        self.assertEqual(self.upload()[0], 201)
        before = self.catalog.read_bytes()
        for field in ("city", "interests"):
            payload = {"city": "Minneapolis", "interests": "jazz"}
            payload[field] = "\ud800"
            raw = json.dumps(payload).encode("utf-8")
            with self.subTest(field=field):
                code, body, _headers = self.request("POST", "/api/shortlist", raw,
                                                   {"Content-Type": "application/json"})
                self.assertEqual((code, body), (422, {"error": "invalid_search"}))
        self.assertEqual(self.catalog.read_bytes(), before)
        self.assertEqual(self.search()[0], 200)

    def test_subprocess_launch_from_independent_app_root(self):
        cmd = [sys.executable, str(ROOT / "web_app.py"), "--catalog", str(self.catalog), "--port", "0"]
        proc = subprocess.Popen(cmd, cwd=ROOT, env={"PYTHONPATH": "", "PATH": os.environ.get("PATH", "")},
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            address = proc.stdout.readline().strip()
            self.assertTrue(address.startswith("Local CultureFit catalogue: http://127.0.0.1:"))
            port = int(address.rsplit(':', 1)[-1])
            client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            client.request("GET", "/api/catalog", headers={"Host": f"127.0.0.1:{port}"})
            self.assertEqual(client.getresponse().status, 200)
            client.close()
        finally:
            proc.terminate()
            proc.communicate(timeout=5)

    def _filtered_search(self, **options):
        body = {"city": "Minneapolis", "interests": "jazz,coffee", "per_lane": 3}
        body.update(options)
        return self.request("POST", "/api/shortlist", json.dumps(body).encode("utf-8"),
                            {"Content-Type": "application/json"})

    def test_required_and_excluded_filters_use_real_catalogue_lanes(self):
        self.assertEqual(self.upload()[0], 201)
        code, value, _ = self._filtered_search(required_tags="JAZZ", excluded_tags="COFFEE")
        self.assertEqual(code, 200)
        self.assertEqual(value["required_tags"], ["jazz"])
        self.assertEqual(value["excluded_tags"], ["coffee"])
        self.assertEqual(value["missing_lanes"], ["place", "brand"])
        self.assertEqual(value["lanes"]["artist"][0]["name"], "Sample Artist")
        self.assertEqual(value["lanes"]["artist"][0]["source_label"], "Artist website")
        self.assertEqual(value["provider_calls"], 0)

    def test_invalid_filters_reject_and_following_valid_search_works(self):
        self.assertEqual(self.upload()[0], 201)
        for filters in ({"required_tags": None},
                        {"excluded_tags": 99},
                        {"required_tags": "jazz", "excluded_tags": "jazz"},
                        {"required_tags": "jazz,"}):
            with self.subTest(filters=filters):
                code, value, _ = self._filtered_search(**filters)
                self.assertEqual((code, value), (422, {"error": "invalid_search"}))
        self.assertEqual(self.search()[0], 200)

    def test_unspecified_filters_retain_prior_search_behavior(self):
        self.assertEqual(self.upload()[0], 201)
        code, value, _ = self.search()
        self.assertEqual(code, 200)
        self.assertEqual([len(value["lanes"][x]) for x in ("place", "brand", "artist")],
                         [1, 1, 1])
        self.assertEqual(value["required_tags"], [])
        self.assertEqual(value["excluded_tags"], [])


    def _coverage_request(self, payload, extra=None):
        headers = {"Content-Type": "application/json"}
        if extra:
            headers.update(extra)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return self.request("POST", "/api/coverage", data, headers)

    def test_coverage_api_counts_operator_records_without_provider_claims(self):
        self.assertEqual(self.upload()[0], 201)
        code, report, headers = self._coverage_request(
            {"city": "MINNEAPOLIS", "interests": "Jazz,coffee"})
        self.assertEqual(code, 200)
        self.assertEqual(report["format"], "culturefit_operator_catalog_coverage_v1")
        self.assertEqual(report["interests"], ["jazz", "coffee"])
        self.assertEqual(report["total_records"], 3)
        self.assertEqual(report["lanes"]["place"]["eligible_records"], 1)
        self.assertEqual(report["lanes"]["place"]["matching_all_interests"], 1)
        self.assertEqual(report["lanes"]["brand"]["per_interest"]["coffee"], 1)
        self.assertEqual(report["lanes"]["artist"]["per_interest"]["jazz"], 1)
        self.assertEqual(report["uncovered_lane_interests"], [
            {"lane": "brand", "interest": "jazz"},
            {"lane": "artist", "interest": "coffee"}])
        self.assertEqual(report["provider_calls"], 0)
        self.assertFalse(report["provider_independently_verified"])
        self.assertEqual(report["source_status"], "user_supplied_unverified")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.catalog.read_bytes(), DATA, "coverage must not write CSV")

    def test_coverage_api_preserves_existing_origin_host_and_missing_catalogue_guards(self):
        self.assertEqual(
            self._coverage_request({"city": "Minneapolis", "interests": "jazz"})[0], 409)
        self.upload()
        before = self.catalog.read_bytes()
        valid = {"city": "Minneapolis", "interests": "jazz"}
        code, _, _ = self._coverage_request(valid, {"Origin": "https://untrusted.example"})
        self.assertEqual(code, 403)
        code, _, _ = self._coverage_request(valid, {"Host": "untrusted.example"})
        self.assertEqual(code, 403)
        self.assertEqual(self.catalog.read_bytes(), before)

    def test_coverage_api_rejects_malformed_and_ambiguous_queries_without_mutation(self):
        self.upload()
        before = self.catalog.read_bytes()
        for payload in (
            {}, {"city": "Minneapolis"}, {"city": "Minneapolis", "interests": "jazz", "live": True},
            {"city": 42, "interests": "jazz"},
            {"city": "Minneapolis", "interests": ""},
            {"city": "Minneapolis", "interests": "jazz,"},
            b'{"city":"Minneapolis","interests":"jazz","interests":"coffee"}',
            b'{"city":"Minneapolis","interests":NaN}',
            b'{"city":"\\ud800","interests":"jazz"}',
        ):
            with self.subTest(payload=payload):
                code, body, _ = self._coverage_request(payload)
                self.assertEqual((code, body), (422, {"error": "invalid_coverage"}))
        self.assertEqual(self.catalog.read_bytes(), before)
        self.assertEqual(
            self._coverage_request({"city": "Minneapolis", "interests": "jazz"})[0], 200)



    def _explain_request(self, body, extra=None):
        headers = {"Content-Type": "application/json"}
        if extra:
            headers.update(extra)
        raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        return self.request("POST", "/api/explain", raw, headers)

    def test_explain_api_returns_actual_csv_source_and_rank(self):
        self.assertEqual(self.upload()[0], 201)
        request = {"lane": "place", "name": "Sample Venue", "city": "MINNEAPOLIS",
                   "candidate_city": "Minneapolis", "interests": "coffee,jazz", "per_lane": 2}
        code, payload, headers = self._explain_request(request)
        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "included")
        self.assertEqual(payload["shortlist_rank"], 1)
        self.assertEqual(payload["matched_interests"], ["coffee", "jazz"])
        self.assertEqual(payload["candidate"]["source_url"], "https://example.org/venue")
        self.assertEqual(payload["candidate"]["source_label"], "Operator notes")
        self.assertEqual(payload["provider_calls"], 0)
        self.assertFalse(payload["provider_independently_verified"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_explain_api_classifies_excluded_tags_without_touching_catalogue(self):
        self.assertEqual(self.upload()[0], 201)
        previous = self.catalog.read_bytes()
        code, payload, _ = self._explain_request({
            "lane": "place", "name": "Sample Venue", "city": "Minneapolis",
            "interests": "jazz,coffee", "excluded_tags": "jazz"})
        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "ineligible")
        self.assertEqual(payload["reasons"], ["excluded_tags_present"])
        self.assertEqual(payload["excluded_tags_present"], ["jazz"])
        self.assertEqual(self.catalog.read_bytes(), previous)

    def test_explain_api_handles_cutoff_and_missing_records(self):
        expanded = DATA + (b"place,Another Venue,Minneapolis,jazz;coffee,"
                           b"https://example.org/another,Operator notes\n")
        self.assertEqual(self.upload(expanded)[0], 201)
        request = {"lane": "place", "name": "Sample Venue", "city": "Minneapolis",
                   "interests": "jazz,coffee", "per_lane": 1}
        code, payload, _ = self._explain_request(request)
        self.assertEqual((code, payload["status"]), (200, "eligible_outside_result_limit"))
        self.assertEqual(payload["reasons"], ["outside_per_lane_limit"])
        code, body, _ = self._explain_request({**request, "name": "Absent"})
        self.assertEqual((code, body), (422, {"error": "invalid_explanation"}))

    def test_explain_api_missing_catalogue_and_host_origin_restrictions(self):
        req = {"lane": "brand", "name": "Sample Brand", "city": "Chicago",
               "interests": "coffee"}
        self.assertEqual(self._explain_request(req)[0], 409)
        self.assertEqual(self.upload()[0], 201)
        prior = self.catalog.read_bytes()
        self.assertEqual(self._explain_request(req, {"Host": "evil.example"})[0], 403)
        self.assertEqual(self._explain_request(req, {"Origin": "http://evil.example"})[0], 403)
        code, payload, _ = self._explain_request(req)
        self.assertEqual(code, 200)
        self.assertEqual(payload["candidate"]["source_label"], "Operator notes")
        self.assertEqual(self.catalog.read_bytes(), prior)

    def test_explain_api_rejects_ambiguous_json_and_invalid_types(self):
        self.assertEqual(self.upload()[0], 201)
        prior = self.catalog.read_bytes()
        correct = {"lane": "place", "name": "Sample Venue",
                   "city": "Minneapolis", "interests": "jazz,coffee"}
        for bad in [
            {}, {"lane": "place", "name": "Sample Venue"},
            {**correct, "provider_mode": "live"},
            {**correct, "name": None},
            {**correct, "per_lane": True},
            {**correct, "candidate_city": 13},
            {**correct, "required_tags": []},
            {**correct, "interests": "\ud800"},
            b'{"lane":"place","name":"Sample Venue","city":"Minneapolis","interests":"jazz","interests":"coffee"}',
            b'{"lane":"place","name":"Sample Venue","city":"Minneapolis","interests":NaN}',
        ]:
            with self.subTest(bad=bad):
                code, error, _ = self._explain_request(bad)
                self.assertEqual((code, error), (422, {"error": "invalid_explanation"}))
        self.assertEqual(self.catalog.read_bytes(), prior)
        self.assertEqual(self._explain_request(correct)[0], 200)


    def test_bundle_api_returns_real_csv_candidates_and_matches_cli_engine(self):
        from curated_catalog import read_catalog
        from fit_bundle import compose_bundles

        self.assertEqual(self.upload()[0], 201)
        original = self.catalog.read_bytes()
        query = {"city": "Minneapolis", "interests": "jazz,coffee",
                 "per_lane": 3, "max_bundles": 5}
        code, result, headers = self.request(
            "POST", "/api/bundles", json.dumps(query).encode("utf-8"),
            {"Content-Type": "application/json"})
        self.assertEqual(code, 200)
        self.assertEqual(result, compose_bundles(read_catalog(self.catalog), **query))
        self.assertEqual(result["candidate_pool_counts"],
                         {"place": 1, "brand": 1, "artist": 1})
        self.assertEqual(len(result["bundles"]), 1)
        members = result["bundles"][0]["members"]
        self.assertEqual(list(members), ["place", "brand", "artist"])
        self.assertEqual(members["place"]["source_url"], "https://example.org/venue")
        self.assertEqual(members["brand"]["source_label"], "Operator notes")
        self.assertEqual(members["artist"]["name"], "Sample Artist")
        self.assertEqual(result["source_status"], "user_supplied_unverified")
        self.assertIs(result["provider_independently_verified"], False)
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.catalog.read_bytes(), original)

    def test_bundle_api_missing_lane_remains_empty_without_fabricated_result(self):
        self.assertEqual(self.upload()[0], 201)
        query = {"city": "Chicago", "interests": "jazz,coffee"}
        code, result, _ = self.request(
            "POST", "/api/bundles", json.dumps(query).encode("utf-8"),
            {"Content-Type": "application/json"})
        self.assertEqual(code, 200)
        self.assertEqual(result["missing_lanes"], ["place"])
        self.assertEqual(result["bundles"], [])
        self.assertEqual(result["provider_calls"], 0)

    def test_bundle_api_rejects_duplicate_ambiguous_and_invalid_queries(self):
        self.assertEqual(self.upload()[0], 201)
        original = self.catalog.read_bytes()
        query = {"city": "Minneapolis", "interests": "jazz,coffee"}
        ambiguous = [
            b'{"city":"Minneapolis","city":"Chicago","interests":"jazz"}',
            b'{"city":"Minneapolis","interests":"jazz","interests":"coffee"}',
            b'{"city":"Minneapolis","interests":"jazz","max_bundles":2,"max_bundles":3}',
            b'{"city":"Minneapolis","interests":"jazz","unknown":{"a":1,"a":2}}',
        ]
        invalid = [
            {}, {"city": "Minneapolis"}, {**query, "provider_mode": "live"},
            {**query, "max_bundles": True}, {**query, "max_bundles": 11},
            {**query, "per_lane": 0}, {**query, "per_lane": 2.5},
            {**query, "required_tags": ["jazz"]},
            {**query, "required_tags": "jazz", "excluded_tags": "JAZZ"},
            {**query, "city": "\ud800"},
        ]
        for body in ambiguous + [json.dumps(x).encode("utf-8") for x in invalid] + [
            b'{"city":"Minneapolis","interests":NaN}', b'{not-json']:
            with self.subTest(body=body):
                code, error, _ = self.request(
                    "POST", "/api/bundles", body,
                    {"Content-Type": "application/json"})
                self.assertEqual((code, error), (422, {"error": "invalid_bundles"}))
        self.assertEqual(self.catalog.read_bytes(), original)
        self.assertEqual(self.request(
            "POST", "/api/bundles", json.dumps(query).encode("utf-8"),
            {"Content-Type": "application/json"})[0], 200)

    def test_bundle_api_preserves_host_origin_and_missing_catalogue_guards(self):
        body = b'{"city":"Minneapolis","interests":"jazz"}'
        headers = {"Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/api/bundles", body, headers)[0], 409)
        self.assertEqual(self.request(
            "POST", "/api/bundles", body,
            {**headers, "Host": "evil.example"})[0], 403)
        self.assertEqual(self.request(
            "POST", "/api/bundles", body,
            {**headers, "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.upload()[0], 201)
        original = self.catalog.read_bytes()
        self.assertEqual(self.request(
            "POST", "/api/bundles", body,
            {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/bundles", body, headers)[0], 200)
        self.assertEqual(self.catalog.read_bytes(), original)

    def test_bundle_api_prefers_shared_tag_coverage_from_real_records(self):
        alternative = DATA + (
            b"artist,Jazz Coffee Duo,,jazz;coffee,https://example.org/duo,Operator notes\n"
        )
        self.assertEqual(self.upload(alternative)[0], 201)
        request = {"city": "Minneapolis", "interests": "jazz,coffee",
                   "per_lane": 3, "max_bundles": 2}
        code, report, _ = self.request(
            "POST", "/api/bundles", json.dumps(request).encode("utf-8"),
            {"Content-Type": "application/json"})
        self.assertEqual(code, 200)
        self.assertEqual(len(report["bundles"]), 2)
        self.assertEqual(report["bundles"][0]["members"]["artist"]["name"],
                         "Jazz Coffee Duo")
        self.assertEqual(report["bundles"][0]["covered_interests"], ["jazz", "coffee"])
        self.assertEqual(report["bundles"][0]["shared_interests"], ["jazz", "coffee"])
        self.assertEqual(self.catalog.read_bytes(), alternative)

    def _partial_request(self, *, declared=100, payload=b"x",
                         close_write=False, drip=False):
        """Send a real bounded localhost request with an incomplete body."""
        headers = (
            f"POST /api/shortlist HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            f"Origin: http://127.0.0.1:{self.port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {declared}\r\n\r\n"
        ).encode("ascii")
        with patch.object(web_app, "BODY_READ_TIMEOUT_SECONDS", 0.3):
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
                conn.settimeout(3)
                conn.sendall(headers + payload)
                if close_write:
                    conn.shutdown(socket.SHUT_WR)
                stopped = threading.Event()
                sender = None
                if drip:
                    def send_slowly():
                        while not stopped.is_set():
                            try:
                                conn.sendall(b"x")
                            except OSError:
                                break
                            time.sleep(0.04)
                    sender = threading.Thread(target=send_slowly, daemon=True)
                    sender.start()
                try:
                    started = time.monotonic()
                    response = conn.recv(4096)
                    duration = time.monotonic() - started
                finally:
                    stopped.set()
                    if sender is not None:
                        sender.join(timeout=1)
        return int(response.split(b" ")[1]), duration

    def test_stalled_body_has_finite_deadline_and_preserves_catalogue(self):
        self.assertEqual(self.upload()[0], 201)
        original = self.catalog.read_bytes()
        status, elapsed = self._partial_request()
        self.assertEqual(status, 408)
        self.assertLess(elapsed, 1.5)
        self.assertEqual(self.catalog.read_bytes(), original)
        self.assertEqual(self.search()[0], 200)

    def test_slow_drip_cannot_keep_body_reader_open_forever(self):
        self.assertEqual(self.upload()[0], 201)
        status, elapsed = self._partial_request(payload=b"", drip=True)
        self.assertEqual(status, 408)
        self.assertLess(elapsed, 1.5)
        self.assertEqual(self.search()[0], 200)

    def test_early_eof_body_rejected_and_followup_succeeds(self):
        self.assertEqual(self.upload()[0], 201)
        original = self.catalog.read_bytes()
        status, _ = self._partial_request(close_write=True)
        self.assertEqual(status, 400)
        self.assertEqual(self.catalog.read_bytes(), original)
        self.assertEqual(self.search()[0], 200)

if __name__ == "__main__":
    unittest.main()
