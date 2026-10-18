"""Vercel's standard-library HTTP entrypoint; offline demonstration only."""
import json
from http.server import BaseHTTPRequestHandler
from hosted_demo import execute

MAX_REQUEST_BYTES = 4200000

class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return
    def respond(self, code, result):
        raw = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(raw)
    def do_POST(self):
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin not in (None, "https://" + host, "http://" + host):
            return self.respond(403, {"error": "invalid_origin"})
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            return self.respond(415, {"error": "json_only"})
        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdecimal() or len(length) > 8:
            return self.respond(411, {"error": "length_required"})
        if not 0 < int(length) <= MAX_REQUEST_BYTES:
            return self.respond(413, {"error": "body_too_large"})
        try:
            def unique_fields(items):
                value = {}
                for key, item in items:
                    if key in value:
                        raise ValueError("duplicate field")
                    value[key] = item
                return value
            self.connection.settimeout(5)
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                return self.respond(400, {"error": "incomplete_body"})
            value = json.loads(raw, object_pairs_hook=unique_fields,
                               parse_constant=lambda _x: (_ for _ in ()).throw(ValueError("nonfinite")))
            code, result = execute(value)
        except TimeoutError:
            self.close_connection = True
            return self.respond(408, {"error": "body_timeout"})
        except (ValueError, UnicodeError):
            return self.respond(422, {"error": "invalid_demo_request"})
        except Exception:
            return self.respond(502, {"error": "demo_unavailable"})
        return self.respond(code, result)
    def do_GET(self):
        return self.respond(405, {"error": "post_only"})
