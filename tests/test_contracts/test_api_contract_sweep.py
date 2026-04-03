"""
OpenAPI-driven API contract sweep.

Goals:
  - hit the documented read surface and catch 500s / response-shape drift
  - synthesize invalid payloads from request schemas and assert graceful rejection
  - exercise a safe allowlist of valid create flows with cleanup
  - emit artifacts so regressions are triageable after a broad sweep
"""

import json
import os
import re
import uuid
from pathlib import Path

import pytest
import requests

from audit_manifest import (
    INVALID_WRITE_SKIP_PATHS,
    INVALID_WRITE_SKIP_PREFIXES,
    READ_SKIP_PATHS,
    READ_SKIP_PREFIXES,
    canonical_api_path,
    is_download_or_stream,
)
from audit_utils import write_json_artifact
from helpers import auth_headers


def _load_env():
    env_file = Path(__file__).resolve().parents[1] / ".env.test"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ[key.strip()] = value.strip()


_load_env()

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
HTTP_TIMEOUT = (10, 20)
_OPENAPI_CACHE = None


def _openapi():
    global _OPENAPI_CACHE
    if _OPENAPI_CACHE is None:
        resp = requests.get(f"{BASE_URL}/openapi.json", timeout=20)
        resp.raise_for_status()
        _OPENAPI_CACHE = resp.json()
    return _OPENAPI_CACHE


def _resolve_ref(spec, schema):
    while schema and "$ref" in schema:
        ref = schema["$ref"].lstrip("#/").split("/")
        node = spec
        for part in ref:
            node = node[part]
        schema = node
    return schema or {}


def _schema_type(spec, schema):
    schema = _resolve_ref(spec, schema)
    if "type" in schema:
        return schema["type"]
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema and schema[key]:
            return _schema_type(spec, schema[key][0])
    if "properties" in schema:
        return "object"
    return None


def _response_schema(spec, operation, status_code):
    response = operation.get("responses", {}).get(str(status_code))
    if not response:
        response = operation.get("responses", {}).get("200") or operation.get("responses", {}).get("201")
    if not response:
        return None
    content = response.get("content", {})
    payload = content.get("application/json")
    if not payload:
        return None
    return payload.get("schema")


def _request_schema(spec, operation, content_type):
    request_body = operation.get("requestBody", {})
    content = request_body.get("content", {})
    payload = content.get(content_type)
    if not payload:
        return None
    return _resolve_ref(spec, payload.get("schema"))


def _json_matches_schema(spec, schema, payload):
    expected = _schema_type(spec, schema)
    if expected == "object":
        return isinstance(payload, dict)
    if expected == "array":
        return isinstance(payload, list)
    if expected == "string":
        return isinstance(payload, str)
    if expected == "integer":
        return isinstance(payload, int) and not isinstance(payload, bool)
    if expected == "number":
        return isinstance(payload, (int, float)) and not isinstance(payload, bool)
    if expected == "boolean":
        return isinstance(payload, bool)
    return True


def _sample_query_value(param):
    schema = param.get("schema", {})
    if schema.get("default") is not None:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    typ = schema.get("type")
    if typ == "integer":
        return 1
    if typ == "number":
        return 1
    if typ == "boolean":
        return "true"
    return "audit"


def _sample_resource_ids(headers):
    def first_id(path):
        resp = requests.get(f"{BASE_URL}{path}", headers=headers, timeout=15)
        if resp.status_code != 200:
            return 1
        payload = resp.json()
        if isinstance(payload, list) and payload:
            return payload[0].get("id", 1)
        if isinstance(payload, dict):
            for key in ("items", "printers", "jobs", "models", "products", "orders", "spools", "alerts", "profiles", "archives", "projects"):
                if isinstance(payload.get(key), list) and payload[key]:
                    return payload[key][0].get("id", 1)
        return 1

    return {
        "printer_id": first_id("/api/printers"),
        "job_id": first_id("/api/jobs"),
        "model_id": first_id("/api/models"),
        "product_id": first_id("/api/products"),
        "order_id": first_id("/api/orders"),
        "spool_id": first_id("/api/spools"),
        "alert_id": first_id("/api/alerts"),
        "profile_id": first_id("/api/profiles"),
        "archive_id": first_id("/api/archives"),
        "project_id": first_id("/api/projects"),
        "schedule_id": 1,
        "user_id": 1,
        "org_id": 1,
        "webhook_id": 1,
        "detection_id": 1,
        "model_file_id": 1,
        "file_id": 1,
        "camera_id": 1,
        "tag": "audit",
    }


def _materialize_path(path, sample_ids):
    materialized = path
    for param_name, value in sample_ids.items():
        materialized = materialized.replace("{" + param_name + "}", str(value))

    def replace_param(match):
        name = match.group(1)
        lowered = name.lower()
        if "filename" in lowered:
            return "sample.jpg"
        if "tag" in lowered:
            return "audit"
        return "1"

    return re.sub(r"\{([^}]+)\}", replace_param, materialized)


def _should_skip_read(path):
    canonical = canonical_api_path(path)
    return canonical in READ_SKIP_PATHS or any(canonical.startswith(prefix) for prefix in READ_SKIP_PREFIXES)


def _should_skip_invalid_write(path):
    canonical = canonical_api_path(path)
    return canonical in INVALID_WRITE_SKIP_PATHS or any(canonical.startswith(prefix) for prefix in INVALID_WRITE_SKIP_PREFIXES)


def _request_json(method, path, headers, params=None, json_body=None):
    return requests.request(
        method,
        f"{BASE_URL}{path}",
        headers=headers,
        params=params,
        json=json_body,
        timeout=HTTP_TIMEOUT,
        stream=True,
    )


def _sample_from_schema(spec, schema, depth=0):
    schema = _resolve_ref(spec, schema)
    if not schema or depth > 3:
        return {}
    if schema.get("enum"):
        return schema["enum"][0]
    if schema.get("default") is not None:
        return schema["default"]

    schema_type = _schema_type(spec, schema)
    if schema_type == "object":
        props = schema.get("properties", {})
        required = schema.get("required") or list(props.keys())[:3]
        return {
            name: _sample_from_schema(spec, props.get(name, {}), depth + 1)
            for name in required
            if name in props
        }
    if schema_type == "array":
        item_schema = schema.get("items", {})
        return [_sample_from_schema(spec, item_schema, depth + 1)]
    if schema_type == "string":
        fmt = schema.get("format")
        if fmt == "email":
            return "audit@example.com"
        if fmt == "date":
            return "2026-03-28"
        if fmt == "date-time":
            return "2026-03-28T12:00:00Z"
        return "audit"
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1.5
    if schema_type == "boolean":
        return True
    return "audit"


def _wrong_type_value(spec, schema):
    schema = _resolve_ref(spec, schema)
    schema_type = _schema_type(spec, schema)
    if schema_type == "object":
        props = schema.get("properties", {})
        names = schema.get("required") or list(props.keys())[:3]
        return {
            name: _wrong_type_value(spec, props.get(name, {}))
            for name in names
            if name in props
        }
    if schema_type == "array":
        return "not-an-array"
    if schema_type in {"integer", "number"}:
        return "not-a-number"
    if schema_type == "boolean":
        return "not-a-bool"
    if schema_type == "string":
        return 12345
    return None


def _invalid_payload_cases(spec, operation):
    cases = []
    json_schema = _request_schema(spec, operation, "application/json")
    form_schema = _request_schema(spec, operation, "application/x-www-form-urlencoded")

    if json_schema:
        required = json_schema.get("required", [])
        valid = _sample_from_schema(spec, json_schema)
        wrong_types = _wrong_type_value(spec, json_schema)
        if required:
            cases.append({"kind": "json", "label": "missing-required", "body": {}, "should_reject": True})
        elif _schema_type(spec, json_schema) == "object":
            cases.append({"kind": "json", "label": "empty-object", "body": {}, "should_reject": False})
        cases.append({"kind": "json", "label": "wrong-types", "body": wrong_types if wrong_types is not None else {"value": None}, "should_reject": True})
        if isinstance(valid, dict) and valid:
            missing_one = dict(valid)
            missing_one.pop(next(iter(missing_one)))
            cases.append({"kind": "json", "label": "partial-object", "body": missing_one, "should_reject": bool(required)})

    if form_schema:
        required = form_schema.get("required", [])
        cases.append({"kind": "form", "label": "empty-form", "body": {}, "should_reject": bool(required)})
        wrong = {}
        for name in required[:3]:
            wrong[name] = ""
        if wrong:
            cases.append({"kind": "form", "label": "blank-required-fields", "body": wrong, "should_reject": True})

    return cases


def _safe_create_payloads():
    suffix = uuid.uuid4().hex[:8]
    return [
        {
            "method": "POST",
            "path": "/api/printers",
            "body": {
                "name": f"Audit Printer {suffix}",
                "model": "Bambu Lab X1C",
                "api_type": "bambu",
                "slot_count": 4,
                "is_active": True,
            },
            "cleanup": lambda created_id, headers: requests.delete(f"{BASE_URL}/api/printers/{created_id}", headers=headers, timeout=15),
            "skip_statuses": {400, 403},
        },
        {
            "method": "POST",
            "path": "/api/models",
            "body": {
                "name": f"Audit Model {suffix}",
                "build_time_hours": 1.5,
            },
            "cleanup": lambda created_id, headers: requests.delete(f"{BASE_URL}/api/models/{created_id}", headers=headers, timeout=15),
            "skip_statuses": set(),
        },
        {
            "method": "POST",
            "path": "/api/products",
            "body": {
                "name": f"Audit Product {suffix}",
                "price": 12.5,
            },
            "cleanup": lambda created_id, headers: requests.delete(f"{BASE_URL}/api/products/{created_id}", headers=headers, timeout=15),
            "skip_statuses": set(),
        },
        {
            "method": "POST",
            "path": "/api/jobs",
            "body": {
                "item_name": f"Audit Job {suffix}",
                "priority": 3,
            },
            "cleanup": lambda created_id, headers: requests.delete(f"{BASE_URL}/api/jobs/{created_id}", headers=headers, timeout=15),
            "skip_statuses": set(),
        },
    ]


def test_openapi_read_endpoints_return_documented_shapes(admin_token):
    headers = auth_headers(admin_token)
    spec = _openapi()
    sample_ids = _sample_resource_ids(headers)
    failures = []
    results = []
    seen = set()

    try:
        for path, path_item in sorted(spec.get("paths", {}).items()):
            canonical = canonical_api_path(path)
            if canonical in seen:
                continue
            seen.add(canonical)
            if _should_skip_read(path):
                results.append({"path": path, "status": "skipped", "reason": "skip-list"})
                continue
            operation = path_item.get("get")
            if not operation:
                continue

            params = {}
            for param in operation.get("parameters", []):
                if param.get("in") == "query" and param.get("required"):
                    params[param["name"]] = _sample_query_value(param)

            materialized_path = _materialize_path(path, sample_ids)
            resp = None
            entry = {
                "method": "GET",
                "path": materialized_path,
                "canonical_path": canonical,
                "query_params": params,
            }
            try:
                resp = _request_json("GET", materialized_path, headers, params=params)
                entry["status_code"] = resp.status_code
                entry["content_type"] = resp.headers.get("content-type", "")
                if resp.status_code >= 500:
                    failures.append(f"GET {materialized_path} returned {resp.status_code}")
                    entry["result"] = "server-error"
                    results.append(entry)
                    continue

                if is_download_or_stream(materialized_path):
                    entry["result"] = "stream-or-download"
                    results.append(entry)
                    continue

                if resp.headers.get("content-type", "").startswith("application/json") and resp.status_code in (200, 201):
                    schema = _response_schema(spec, operation, resp.status_code)
                    if schema and not _json_matches_schema(spec, schema, resp.json()):
                        expected = _schema_type(spec, schema)
                        failures.append(f"GET {materialized_path} returned JSON that did not match top-level type {expected}")
                        entry["result"] = "shape-mismatch"
                    else:
                        entry["result"] = "ok"
                else:
                    entry["result"] = "non-json-ok"
                results.append(entry)
            except requests.RequestException as exc:
                entry["result"] = "request-failed"
                entry["error"] = str(exc)
                results.append(entry)
                failures.append(f"GET {materialized_path} request failed: {exc}")
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
    finally:
        write_json_artifact(
            "audit",
            "contracts",
            "read_endpoints.json",
            payload={
                "base_url": BASE_URL,
                "total_results": len(results),
                "failures": failures,
                "results": results,
            },
        )

    if failures:
        pytest.fail("\n".join(failures))


def test_mutating_endpoints_reject_invalid_payloads_without_500(admin_token):
    headers = auth_headers(admin_token)
    spec = _openapi()
    sample_ids = _sample_resource_ids(headers)
    failures = []
    results = []
    seen = set()

    try:
        for path, path_item in sorted(spec.get("paths", {}).items()):
            canonical = canonical_api_path(path)
            if canonical in seen:
                continue
            seen.add(canonical)
            if _should_skip_invalid_write(path):
                continue

            for method in ("post", "put", "patch"):
                operation = path_item.get(method)
                if not operation:
                    continue
                materialized_path = _materialize_path(path, sample_ids)
                cases = _invalid_payload_cases(spec, operation)
                if not cases:
                    continue

                for case in cases:
                    entry = {
                        "method": method.upper(),
                        "path": materialized_path,
                        "case": case["label"],
                        "kind": case["kind"],
                        "should_reject": case["should_reject"],
                    }
                    resp = None
                    try:
                        if case["kind"] == "json":
                            resp = _request_json(method.upper(), materialized_path, headers, json_body=case["body"])
                        else:
                            resp = requests.request(
                                method.upper(),
                                f"{BASE_URL}{materialized_path}",
                                headers=headers,
                                data=case["body"],
                                timeout=20,
                            )
                        entry["status_code"] = resp.status_code
                        if resp.status_code >= 500:
                            failures.append(f"{method.upper()} {materialized_path} returned {resp.status_code} for invalid payload ({case['label']})")
                            entry["result"] = "server-error"
                        elif case["should_reject"] and 200 <= resp.status_code < 300:
                            failures.append(f"{method.upper()} {materialized_path} accepted invalid payload ({case['label']}) with {resp.status_code}")
                            entry["result"] = "accepted-invalid"
                        else:
                            entry["result"] = "rejected-or-tolerated"
                    except requests.RequestException as exc:
                        entry["result"] = "request-failed"
                        entry["error"] = str(exc)
                        failures.append(f"{method.upper()} {materialized_path} invalid payload case {case['label']} failed: {exc}")
                    finally:
                        results.append(entry)
                        if resp is not None:
                            try:
                                resp.close()
                            except Exception:
                                pass
    finally:
        write_json_artifact(
            "audit",
            "contracts",
            "invalid_writes.json",
            payload={
                "base_url": BASE_URL,
                "total_results": len(results),
                "failures": failures,
                "results": results,
            },
        )

    if failures:
        pytest.fail("\n".join(failures))


def test_safe_create_endpoints_accept_valid_payloads(admin_token):
    headers = auth_headers(admin_token)
    failures = []
    results = []

    try:
        for case in _safe_create_payloads():
            entry = {
                "method": case["method"],
                "path": case["path"],
                "request_body": case["body"],
            }
            resp = _request_json(case["method"], case["path"], headers, json_body=case["body"])
            entry["status_code"] = resp.status_code
            if resp.status_code in case["skip_statuses"]:
                entry["result"] = "skipped-by-status"
                results.append(entry)
                continue
            if resp.status_code not in (200, 201):
                entry["result"] = "create-failed"
                entry["response_excerpt"] = resp.text[:160]
                failures.append(f"{case['method']} {case['path']} returned {resp.status_code}: {resp.text[:160]}")
                results.append(entry)
                continue

            payload = resp.json()
            created_id = payload.get("id") if isinstance(payload, dict) else None
            entry["created_id"] = created_id
            if not created_id:
                entry["result"] = "missing-id"
                failures.append(f"{case['method']} {case['path']} succeeded but did not return an id")
                results.append(entry)
                continue

            cleanup_resp = case["cleanup"](created_id, headers)
            entry["cleanup_status"] = cleanup_resp.status_code
            if cleanup_resp.status_code not in (200, 204):
                entry["result"] = "cleanup-failed"
                failures.append(f"Cleanup failed for {case['path']} id={created_id}: {cleanup_resp.status_code}")
            else:
                entry["result"] = "ok"
            results.append(entry)
    finally:
        write_json_artifact(
            "audit",
            "contracts",
            "safe_creates.json",
            payload={
                "base_url": BASE_URL,
                "total_results": len(results),
                "failures": failures,
                "results": results,
            },
        )

    if failures:
        pytest.fail("\n".join(failures))
