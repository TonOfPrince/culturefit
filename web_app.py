#!/usr/bin/env python3
"""Local source-attributed catalogue with an optional loopback provider connection.

Catalogue operations use operator records without provider I/O. Only /api/plan
can contact an explicitly configured separate local gateway; it owns provider
credentials, authorization and one-shot state. No parent runtime imports.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from curated_catalog import COLUMNS, CatalogError, LANES, MAX_CATALOG_BYTES, read_catalog, shortlist
from catalog_coverage import coverage_report
from candidate_explain import explain_candidate
from fit_bundle import compose_bundles
from city_compare import compare_cities
from catalog_entry_service import catalogue_status, append_entry
from catalog_facets import FacetError, discover_facets
from catalog_impact import record_impact
from provider_connection import MAX_PLAN_BYTES, plan_via_gateway, status_via_gateway, validate_port

ROOT = Path(__file__).resolve().parent
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/ui/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/ui/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
MAX_SEARCH_BYTES = 4096
BODY_READ_TIMEOUT_SECONDS = 5.0
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'none'; object-src 'none'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")


class LocalCatalogueServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, catalogue: str | Path, port: int = 0, *,
                 provider_port: int | None = None):
        validate_port(provider_port)
        self.provider_port = provider_port
        self.catalogue = Path(catalogue).expanduser().absolute()
        if self.catalogue.is_symlink() or not self.catalogue.parent.is_dir():
            raise ValueError("Catalogue parent directory must exist and the file must not be a symlink")
        self.catalogue_lock = threading.RLock()
        super().__init__(("127.0.0.1", port), LocalCatalogueHandler)
        if self.provider_port == self.server_port:
            self.server_close()
            raise ValueError("Provider gateway must use a separate loopback port")


class LocalCatalogueHandler(BaseHTTPRequestHandler):
    """Same-origin loopback HTTP API, serving only named local static assets."""

    def log_message(self, _format: str, *_args: object) -> None:
        # Intentionally never log customer query parameters, catalogue rows, or paths.
        return

    @property
    def app(self) -> LocalCatalogueServer:
        return self.server  # type: ignore[return-value]

    def _headers(self, code: int, kind: str, length: int) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()

    def _bytes(self, code: int, value: bytes, kind: str) -> None:
        self._headers(code, kind, len(value))
        self.wfile.write(value)

    def _json(self, code: int, value: dict) -> None:
        self._bytes(code, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                    "application/json; charset=utf-8")

    def _error(self, code: int, error: str) -> None:
        self._json(code, {"error": error})

    def _host_ok(self) -> bool:
        hostname, port = self.app.server_address
        allowed = {f"{hostname}:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed:
            self._error(403, "invalid_host")
            return False
        return True

    def _origin_ok(self) -> bool:
        hostname, port = self.app.server_address
        allowed = {f"http://{hostname}:{port}", f"http://localhost:{port}"}
        if self.headers.get("Origin") not in allowed:
            self._error(403, "invalid_origin")
            return False
        return True

    def _body(self, limit: int, kind: str) -> bytes | None:
        supplied_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if supplied_type != kind:
            self._error(415, "unsupported_media_type")
            return None
        length = self.headers.get("Content-Length", "")
        if len(length) > 10 or not length.isascii() or not length.isdecimal():
            self._error(411, "length_required")
            return None
        size = int(length)
        if size > limit:
            self._error(413, "body_too_large")
            return None
        if size == 0:
            self._error(422, "empty_body")
            return None
        deadline = time.monotonic() + BODY_READ_TIMEOUT_SECONDS
        received = bytearray()
        try:
            while len(received) < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("request body deadline")
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(min(size - len(received), 65536))
                if not chunk:
                    break
                received.extend(chunk)
        except TimeoutError:
            self.close_connection = True
            self._error(408, "body_timeout")
            return None
        except OSError:
            self.close_connection = True
            self._error(400, "incomplete_body")
            return None
        if len(received) != size:
            self.close_connection = True
            self._error(400, "incomplete_body")
            return None
        return bytes(received)

    def do_GET(self) -> None:
        if not self._host_ok():
            return
        if self.path == "/api/provider-status":
            code, status = status_via_gateway(self.app.provider_port)
            self._json(code, status)
            return
        if self.path == "/api/catalog":
            with self.app.catalogue_lock:
                code, status = catalogue_status(self.app.catalogue)
            self._json(code, status)
            return
        asset = ASSETS.get(self.path)
        if asset is None:
            self._error(404, "not_found")
            return
        name, kind = asset
        try:
            contents = (ROOT / "ui" / name).read_bytes()
        except OSError:
            self._error(503, "asset_unavailable")
            return
        self._bytes(200, contents, kind)

    def do_POST(self) -> None:
        if not self._host_ok() or not self._origin_ok():
            return
        if self.path == "/api/plan":
            self._provider_plan()
        elif self.path == "/api/catalog":
            self._import_catalog()
        elif self.path == "/api/shortlist":
            self._search()
        elif self.path == "/api/coverage":
            self._coverage()
        elif self.path == "/api/facets":
            self._facets()
        elif self.path == "/api/explain":
            self._explain()
        elif self.path == "/api/bundles":
            self._bundles()
        elif self.path == "/api/cities":
            self._cities()
        elif self.path == "/api/impact":
            self._impact()
        elif self.path == "/api/entries":
            self._append_entry()
        else:
            self._error(404, "not_found")

    def _provider_plan(self) -> None:
        """Use the configured gateway without changing its execution authority."""
        data = self._body(MAX_PLAN_BYTES, "application/json")
        if data is None:
            return
        code, result = plan_via_gateway(self.app.provider_port, data)
        self._json(code, result)

    def _append_entry(self) -> None:
        """Add one real authored record only against the current catalogue hash."""
        data = self._body(MAX_SEARCH_BYTES, "application/json")
        if data is None:
            return
        try:
            def unique_fields(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result

            payload = json.loads(
                data.decode("utf-8"), object_pairs_hook=unique_fields,
                parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("invalid constant")))
        except (ValueError, UnicodeError):
            self._error(422, "invalid_entry")
            return

        with self.app.catalogue_lock:
            code, result = append_entry(self.app.catalogue, payload)
        self._json(code, result)

    def _import_catalog(self) -> None:
        data = self._body(MAX_CATALOG_BYTES, "text/csv")
        if data is None:
            return
        if not self.app.catalogue_lock.acquire(timeout=3):
            self._error(503, "catalogue_busy")
            return
        temporary: Path | None = None
        error: tuple[int, str] | None = None
        imported_count = 0
        try:
            target = self.app.catalogue
            if target.is_symlink():
                error = (409, "invalid_catalogue_path")
            elif target.exists() and self.headers.get("X-Replace-Catalogue") != "yes":
                error = (409, "confirmation_required")
            else:
                try:
                    with tempfile.NamedTemporaryFile(mode="wb", prefix=".culturefit-", suffix=".csv",
                                                     dir=target.parent, delete=False) as stream:
                        temporary = Path(stream.name)
                        os.fchmod(stream.fileno(), 0o600)
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    parsed = read_catalog(temporary)
                    os.replace(temporary, target)
                    temporary = None
                    imported_count = len(parsed)
                except (CatalogError, OSError, UnicodeError):
                    error = (422, "invalid_catalogue")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            self.app.catalogue_lock.release()
        # The entire commit/rollback and temporary-file cleanup precede response.
        if error is not None:
            self._error(*error)
        else:
            self._json(201, {"state": "ready", "record_count": imported_count,
                             "source_status": "user_supplied_unverified"})

    def _search(self) -> None:
        data = self._body(MAX_SEARCH_BYTES, "application/json")
        if data is None:
            return
        try:
            def unique_fields(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result

            payload = json.loads(
                data.decode("utf-8"), object_pairs_hook=unique_fields,
                parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("invalid constant")))
            if (not isinstance(payload, dict) or
                    not {"city", "interests"}.issubset(payload) or
                    set(payload) - {"city", "interests", "per_lane",
                                    "required_tags", "excluded_tags"}):
                raise ValueError("unexpected fields")
            count = payload.get("per_lane", 3)
            if type(count) is not int:
                raise ValueError("invalid count")
            if (not isinstance(payload["city"], str) or not isinstance(payload["interests"], str) or
                    any(not isinstance(payload.get(key, ""), str)
                        for key in ("required_tags", "excluded_tags"))):
                raise ValueError("invalid search")
        except (ValueError, UnicodeError):
            self._error(422, "invalid_search")
            return
        with self.app.catalogue_lock:
            if not self.app.catalogue.is_file():
                self._error(409, "catalogue_missing")
                return
            try:
                catalog = read_catalog(self.app.catalogue)
            except CatalogError:
                self._error(503, "catalogue_unavailable")
                return
            try:
                result = shortlist(catalog, city=payload["city"],
                                   interests=payload["interests"], per_lane=count,
                                   required_tags=payload.get("required_tags", ""),
                                   excluded_tags=payload.get("excluded_tags", ""))
            except CatalogError:
                self._error(422, "invalid_search")
                return
        self._json(200, result)


    def _bundles(self) -> None:
        """Compose real three-lane bundles from the current operator CSV."""
        data = self._body(MAX_SEARCH_BYTES, "application/json")
        if data is None:
            return
        try:
            def unique_fields(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result

            payload = json.loads(
                data.decode("utf-8"), object_pairs_hook=unique_fields,
                parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("invalid constant")))
            mandatory = {"city", "interests"}
            allowed = mandatory | {"per_lane", "max_bundles", "required_tags",
                                   "excluded_tags", "unique_places"}
            if (not isinstance(payload, dict) or not mandatory.issubset(payload)
                    or set(payload) - allowed
                    or any(not isinstance(payload[key], str) for key in mandatory)
                    or any(not isinstance(payload.get(key, ""), str)
                           for key in ("required_tags", "excluded_tags"))
                    or any(type(payload.get(key, default)) is not int
                           for key, default in (("per_lane", 8), ("max_bundles", 3)))
                    or type(payload.get("unique_places", False)) is not bool):
                raise ValueError("invalid bundle request")
        except (ValueError, UnicodeError):
            self._error(422, "invalid_bundles")
            return
        with self.app.catalogue_lock:
            if not self.app.catalogue.is_file():
                self._error(409, "catalogue_missing")
                return
            try:
                records = read_catalog(self.app.catalogue)
            except CatalogError:
                self._error(503, "catalogue_unavailable")
                return
            try:
                report = compose_bundles(
                    records, city=payload["city"], interests=payload["interests"],
                    per_lane=payload.get("per_lane", 8),
                    max_bundles=payload.get("max_bundles", 3),
                    unique_places=payload.get("unique_places", False),
                    required_tags=payload.get("required_tags", ""),
                    excluded_tags=payload.get("excluded_tags", ""),
                )
            except CatalogError:
                self._error(422, "invalid_bundles")
                return
        self._json(200, report)

    def _facets(self) -> None:
        """Discover real tags for a city from the current operator CSV."""
        data = self._body(MAX_SEARCH_BYTES, "application/json")
        if data is None:
            return
        try:
            def unique_fields(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result

            payload = json.loads(
                data.decode("utf-8"), object_pairs_hook=unique_fields,
                parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("invalid constant")))
            if (not isinstance(payload, dict) or set(payload) != {"city"}
                    or not isinstance(payload["city"], str)):
                raise ValueError("invalid facet request")
        except (ValueError, UnicodeError):
            self._error(422, "invalid_facets")
            return

        with self.app.catalogue_lock:
            if not self.app.catalogue.is_file():
                self._error(409, "catalogue_missing")
                return
            try:
                records = read_catalog(self.app.catalogue)
            except CatalogError:
                self._error(503, "catalogue_unavailable")
                return
            try:
                report = discover_facets(records, city=payload["city"])
            except FacetError:
                self._error(422, "invalid_facets")
                return
        self._json(200, report)

    def _coverage(self) -> None:
        """Read-only operator source coverage; never provider or URL network I/O."""
        data = self._body(MAX_SEARCH_BYTES, "application/json")
        if data is None:
            return
        try:
            def unique_pairs(pairs):
                values = {}
                for key, value in pairs:
                    if key in values:
                        raise ValueError("duplicate field")
                    values[key] = value
                return values

            payload = json.loads(
                data.decode("utf-8"), object_pairs_hook=unique_pairs,
                parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("invalid constant")))
            if (not isinstance(payload, dict) or set(payload) != {"city", "interests"}
                    or not isinstance(payload["city"], str)
                    or not isinstance(payload["interests"], str)):
                raise ValueError("invalid coverage request")
        except (ValueError, UnicodeError):
            self._error(422, "invalid_coverage")
            return

        with self.app.catalogue_lock:
            if not self.app.catalogue.is_file():
                self._error(409, "catalogue_missing")
                return
            try:
                records = read_catalog(self.app.catalogue)
            except CatalogError:
                self._error(503, "catalogue_unavailable")
                return
            try:
                report = coverage_report(records, city=payload["city"],
                                         interests=payload["interests"])
            except CatalogError:
                self._error(422, "invalid_coverage")
                return
        self._json(200, report)

    def _explain(self) -> None:
        """Explain the ranking decision for one real operator-authored candidate."""
        data = self._body(MAX_SEARCH_BYTES, "application/json")
        if data is None:
            return
        try:
            def unique_fields(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result

            query = json.loads(
                data.decode("utf-8"), object_pairs_hook=unique_fields,
                parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("invalid constant")))
            mandatory = {"lane", "name", "city", "interests"}
            allowed = mandatory | {"candidate_city", "per_lane", "required_tags", "excluded_tags"}
            if (not isinstance(query, dict) or not mandatory.issubset(query)
                    or set(query) - allowed):
                raise ValueError("unexpected explanation fields")
            if (any(not isinstance(query[key], str) for key in mandatory)
                    or any(not isinstance(query.get(key, ""), str)
                           for key in ("required_tags", "excluded_tags"))
                    or (query.get("candidate_city") is not None
                        and not isinstance(query["candidate_city"], str))
                    or type(query.get("per_lane", 3)) is not int):
                raise ValueError("invalid explanation type")
        except (ValueError, UnicodeError):
            self._error(422, "invalid_explanation")
            return

        with self.app.catalogue_lock:
            if not self.app.catalogue.is_file():
                self._error(409, "catalogue_missing")
                return
            try:
                catalogue = read_catalog(self.app.catalogue)
            except CatalogError:
                self._error(503, "catalogue_unavailable")
                return
            try:
                report = explain_candidate(
                    catalogue, lane=query["lane"], name=query["name"],
                    city=query["city"], interests=query["interests"],
                    candidate_city=query.get("candidate_city"),
                    per_lane=query.get("per_lane", 3),
                    required_tags=query.get("required_tags", ""),
                    excluded_tags=query.get("excluded_tags", ""),
                )
            except CatalogError:
                self._error(422, "invalid_explanation")
                return
        self._json(200, report)

    def _cities(self) -> None:
        """Compare actual operator-source place coverage across two named cities."""
        data = self._body(MAX_SEARCH_BYTES, "application/json")
        if data is None:
            return
        try:
            def unique_fields(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result

            payload = json.loads(
                data.decode("utf-8"), object_pairs_hook=unique_fields,
                parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("invalid constant")))
            mandatory = {"first_city", "second_city", "interests"}
            allowed = mandatory | {"per_city"}
            if (not isinstance(payload, dict) or not mandatory.issubset(payload)
                    or set(payload) - allowed
                    or any(not isinstance(payload[key], str) for key in mandatory)
                    or type(payload.get("per_city", 3)) is not int):
                raise ValueError("invalid city comparison request")
        except (ValueError, UnicodeError):
            self._error(422, "invalid_city_comparison")
            return

        with self.app.catalogue_lock:
            if not self.app.catalogue.is_file():
                self._error(409, "catalogue_missing")
                return
            try:
                candidates = read_catalog(self.app.catalogue)
            except CatalogError:
                self._error(503, "catalogue_unavailable")
                return
            try:
                result = compare_cities(
                    candidates, first_city=payload["first_city"],
                    second_city=payload["second_city"],
                    interests=payload["interests"],
                    per_city=payload.get("per_city", 3),
                )
            except CatalogError:
                self._error(422, "invalid_city_comparison")
                return
        self._json(200, result)

    def _impact(self) -> None:
        """Preview one operator-authored record's effect on existing local ranks."""
        data = self._body(MAX_SEARCH_BYTES, "application/json")
        if data is None:
            return
        try:
            def unique_fields(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("duplicate JSON field")
                    value[key] = item
                return value

            payload = json.loads(
                data.decode("utf-8"), object_pairs_hook=unique_fields,
                parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            required = {"row", "city", "interests"}
            permitted = required | {"per_lane", "required_tags", "excluded_tags"}
            row = payload.get("row") if isinstance(payload, dict) else None
            if (not isinstance(payload, dict) or not required.issubset(payload)
                    or set(payload) - permitted or not isinstance(row, dict)
                    or set(row) != set(COLUMNS)
                    or any(not isinstance(row[key], str) for key in COLUMNS)
                    or not isinstance(payload["city"], str)
                    or not isinstance(payload["interests"], str)
                    or any(not isinstance(payload.get(key, ""), str)
                           for key in ("required_tags", "excluded_tags"))
                    or type(payload.get("per_lane", 3)) is not int):
                raise ValueError("invalid impact request")
        except (ValueError, UnicodeError):
            self._error(422, "invalid_impact")
            return

        with self.app.catalogue_lock:
            if not self.app.catalogue.is_file():
                self._error(409, "catalogue_missing")
                return
            try:
                read_catalog(self.app.catalogue)
            except CatalogError:
                self._error(503, "catalogue_unavailable")
                return
            try:
                result = record_impact(
                    self.app.catalogue, row=row, city=payload["city"],
                    interests=payload["interests"],
                    per_lane=payload.get("per_lane", 3),
                    required_tags=payload.get("required_tags", ""),
                    excluded_tags=payload.get("excluded_tags", ""),
                )
            except (CatalogError, ValueError, UnicodeError):
                self._error(422, "invalid_impact")
                return
            except OSError:
                self._error(503, "catalogue_unavailable")
                return
        self._json(200, result)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private localhost CultureFit catalogue browser")
    parser.add_argument("--catalog", required=True,
                        help="Local CSV path for persistent imported records (parent directory must exist)")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--provider-port", type=int, default=None,
                        help="Explicit trusted CultureFit gateway port on 127.0.0.1; absent means locked")
    args = parser.parse_args(argv)
    try:
        validate_port(args.provider_port)
    except ValueError as exc:
        parser.error(str(exc))
    with LocalCatalogueServer(args.catalog, args.port, provider_port=args.provider_port) as server:
        print(f"Local CultureFit catalogue: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
