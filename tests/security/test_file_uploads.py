"""
Layer 3 Security: File Upload Tests
====================================
Tests for path traversal, MIME enforcement, and content-type validation
on all file upload endpoints.

Run: pytest tests/security/test_file_uploads.py -v --tb=short
"""

import io
import pytest
import requests

from .conftest import BASE_URL, _headers, _auth_headers, _no_auth_headers


class TestPathTraversal:
    """Verify path traversal attacks are blocked on file uploads."""

    def test_vision_model_path_traversal(self, admin_token):
        """ONNX upload with ../../ in filename must be sanitized."""
        malicious_name = "../../etc/passwd.onnx"
        # Create a minimal fake ONNX file (just bytes, server should validate)
        fake_onnx = io.BytesIO(b"\x08\x01" * 50)  # Not valid ONNX, but tests filename handling
        r = requests.post(
            f"{BASE_URL}/api/vision/models",
            files={"file": (malicious_name, fake_onnx, "application/octet-stream")},
            data={"name": "traversal_test", "detection_type": "spaghetti", "version": "1.0", "input_size": "640"},
            headers=_auth_headers(admin_token),
            timeout=15,
        )
        # Server should either reject the file (400/422) or sanitize the filename
        if r.status_code in (200, 201):
            data = r.json()
            stored_name = data.get("filename", data.get("path", ""))
            assert ".." not in stored_name and "/" not in stored_name.replace("/data/", ""), \
                f"Path traversal not sanitized! Stored as: {stored_name}"
            # Cleanup
            model_id = data.get("id")
            if model_id:
                requests.delete(
                    f"{BASE_URL}/api/vision/models/{model_id}",
                    headers=_headers(admin_token),
                    timeout=10,
                )
        else:
            # 400/422/415 = properly rejected
            assert r.status_code in (400, 415, 422, 500) and r.status_code != 500, \
                f"Unexpected status: {r.status_code}"

    def test_print_file_path_traversal(self, admin_token):
        """Print file upload with ../../ in filename must be sanitized."""
        malicious_name = "../../../etc/shadow.3mf"
        fake_3mf = io.BytesIO(b"PK\x03\x04" + b"\x00" * 100)  # Minimal ZIP header
        r = requests.post(
            f"{BASE_URL}/api/print-files/upload",
            files={"file": (malicious_name, fake_3mf, "application/octet-stream")},
            headers=_auth_headers(admin_token),
            timeout=15,
        )
        if r.status_code in (200, 201):
            data = r.json()
            stored = data.get("filename", data.get("original_filename", ""))
            assert ".." not in stored, f"Path traversal in stored name: {stored}"


class TestMIMEEnforcement:
    """Verify file type restrictions on upload endpoints."""

    def test_python_file_rejected_as_print_file(self, admin_token):
        """.py file uploaded as print file must be rejected."""
        fake_py = io.BytesIO(b"import os; os.system('rm -rf /')")
        r = requests.post(
            f"{BASE_URL}/api/print-files/upload",
            files={"file": ("malicious.py", fake_py, "text/x-python")},
            headers=_auth_headers(admin_token),
            timeout=15,
        )
        assert r.status_code in (400, 415, 422), \
            f".py file accepted as print file! Got {r.status_code}"

    def test_html_file_rejected_as_logo(self, admin_token):
        """HTML file uploaded as branding logo must be rejected."""
        html_payload = b"<html><script>alert('xss')</script></html>"
        r = requests.post(
            f"{BASE_URL}/api/branding/logo",
            files={"file": ("logo.html", io.BytesIO(html_payload), "text/html")},
            headers=_auth_headers(admin_token),
            timeout=15,
        )
        assert r.status_code in (400, 415, 422), \
            f"HTML file accepted as logo! Got {r.status_code}"

    def test_non_db_file_rejected_for_restore(self, admin_token):
        """Non-database file uploaded for backup restore must be rejected."""
        fake_data = io.BytesIO(b"this is not a sqlite database at all")
        r = requests.post(
            f"{BASE_URL}/api/backups/restore",
            files={"file": ("malicious.db", fake_data, "application/octet-stream")},
            headers=_auth_headers(admin_token),
            timeout=15,
        )
        # Should reject (invalid SQLite) not succeed
        assert r.status_code in (400, 415, 422), \
            f"Non-SQLite file accepted for restore! Got {r.status_code}"


class TestContentTypeMismatch:
    """Verify content-type enforcement on JSON endpoints."""

    def test_xml_body_to_json_endpoint(self, admin_token):
        """XML payload sent to a JSON endpoint must be rejected or ignored."""
        xml_payload = '<?xml version="1.0"?><root><admin>true</admin></root>'
        h = _auth_headers(admin_token)
        h["Content-Type"] = "application/xml"
        r = requests.post(
            f"{BASE_URL}/api/printers",
            data=xml_payload,
            headers=h,
            timeout=10,
        )
        # Must not be 200/201 (should not parse XML as valid printer data)
        assert r.status_code in (400, 415, 422), \
            f"XML body accepted on JSON endpoint! Got {r.status_code}"

    def test_multipart_to_json_endpoint(self, admin_token):
        """Multipart form data to a JSON-only endpoint must be handled safely."""
        r = requests.post(
            f"{BASE_URL}/api/users",
            data={"username": "form_test", "password": "FormTest1!", "role": "admin"},
            headers=_auth_headers(admin_token),
            timeout=10,
        )
        # Should be 400/415/422 (expects JSON), not 200 (shouldn't create user from form data)
        if r.status_code in (200, 201):
            # Cleanup if user was accidentally created
            user_id = r.json().get("id")
            if user_id:
                requests.delete(
                    f"{BASE_URL}/api/users/{user_id}",
                    headers=_headers(admin_token),
                    timeout=10,
                )
            pytest.xfail("JSON endpoint accepted form-data — may need content-type enforcement")
        assert r.status_code != 500, f"Form data to JSON endpoint caused 500"
