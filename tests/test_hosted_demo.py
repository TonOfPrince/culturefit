"""Native HTTP checks of the key-free hosted adapter and isolated requests."""
import http.client
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from hosted_demo import execute
from api.demo import handler
from http.server import ThreadingHTTPServer

CSV = "lane,name,city,tags,source_url,source_label\nplace,Fictional place,Chicago,jazz,https://example.invalid/place,Fictional fixture\nbrand,Fictional brand,,jazz,https://example.invalid/brand,Fictional fixture\nartist,Fictional artist,,jazz,https://example.invalid/artist,Fictional fixture\n"

def payload(path="/api/provider-status", method="GET", body="", csv="", replace=False):
    return {"path":path,"method":method,"body":body,"catalogue_csv":csv,"replace_confirmed":replace}

class HostedDemoTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch("http.client.HTTPSConnection.connect", side_effect=AssertionError("External HTTPS forbidden"))
        self.network = self.guard.start()
        self.addCleanup(self.guard.stop)
        self.addCleanup(self.network.assert_not_called)
    def test_status_is_offline_and_has_no_consumed_attempt(self):
        code, result = execute(payload())
        self.assertEqual(code, 200)
        self.assertEqual(result["mode"], "SYNTHETIC_TRANSPORT_ONLY")
        self.assertFalse(result["attempted"])
    def test_plan_composes_three_explicitly_fictional_lanes(self):
        code, result = execute(payload("/api/plan", "POST", "{}"))
        self.assertEqual(code, 200)
        self.assertEqual({x["lane"] for x in result["evidence_cards"]}, {"place","brand_partner","artist"})
        self.assertTrue(all("Synthetic" in x["source"] for x in result["evidence_cards"]))
        self.assertFalse(result["provider_independently_verified"])
    def test_catalogue_context_is_validated_then_removed(self):
        code, result = execute(payload("/api/catalog", "POST", CSV))
        self.assertEqual(code, 201)
        self.assertEqual(result["record_count"], 3)
        code, result = execute(payload("/api/catalog"))
        self.assertEqual((code,result["state"]), (200,"empty"))
    def test_search_uses_only_the_supplied_current_snapshot(self):
        code, result = execute(payload("/api/shortlist","POST",json.dumps({"city":"Chicago","interests":"jazz","per_lane":3}),CSV))
        self.assertEqual(code, 200)
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual([len(result["lanes"][lane]) for lane in ["place","brand","artist"]], [1,1,1])
        self.assertEqual(execute(payload("/api/shortlist","POST",'{"city":"Chicago","interests":"jazz"}'))[0],409)
    def test_replacement_needs_explicit_confirmation(self):
        self.assertEqual(execute(payload("/api/catalog","POST",CSV,CSV))[0],409)
        self.assertEqual(execute(payload("/api/catalog","POST",CSV,CSV,True))[0],201)
    def test_arbitrary_provider_inputs_are_refused_before_execution(self):
        with patch("hosted_demo.demo_backend") as backend:
            self.assertEqual(execute(payload("/api/plan","POST",'{"seeds":["Other"],"city":"Chicago"}'))[0],422)
            backend.assert_not_called()
    def test_unknown_routes_fields_and_large_context_fail_closed(self):
        values = [payload("/api/elsewhere"), {**payload(),"url":"https://elsewhere.invalid"},
                  payload(csv="x"*2000001), {**payload(),"replace_confirmed":"yes"}]
        with patch("hosted_demo.demo_backend") as backend:
            for value in values:
                self.assertIn(execute(value)[0], (413,422))
            backend.assert_not_called()

class HostedApiTests(unittest.TestCase):
    def setUp(self):
        self.server=ThreadingHTTPServer(("127.0.0.1",0),handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        def close():
            self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)
        self.addCleanup(close)
    def request(self, raw, origin=None, content_type="application/json"):
        c=http.client.HTTPConnection("127.0.0.1",self.server.server_port,timeout=5)
        try:
            headers={"Content-Type":content_type}
            if origin:headers["Origin"]=origin
            c.request("POST","/api/demo",body=raw,headers=headers)
            r=c.getresponse()
            return r.status,json.loads(r.read())
        finally:c.close()
    def test_cross_origin_request_has_no_execution(self):
        with patch("api.demo.execute") as run:
            self.assertEqual(self.request("{}",origin="https://elsewhere.invalid")[0],403)
            run.assert_not_called()
    def test_malformed_duplicate_and_nonfinite_json_are_refused(self):
        for raw in ['{"path":1,"path":2}', '{"x":NaN}', "not-json"]:
            self.assertEqual(self.request(raw)[0],422)
    def test_content_type_and_oversized_request_are_refused(self):
        self.assertEqual(self.request("{}",content_type="text/plain")[0],415)
        c=http.client.HTTPConnection("127.0.0.1",self.server.server_port,timeout=5)
        try:
            c.putrequest("POST","/api/demo")
            c.putheader("Content-Type","application/json");c.putheader("Content-Length","4200001");c.endheaders()
            r=c.getresponse();self.assertEqual(r.status,413);r.read()
        finally:c.close()
    def test_unexpected_failures_do_not_expose_details(self):
        with patch("api.demo.execute",side_effect=RuntimeError("PRIVATE_RAW_DETAILS")):
            code,result=self.request(json.dumps(payload()))
            self.assertEqual((code,result),(502,{"error":"demo_unavailable"}))
