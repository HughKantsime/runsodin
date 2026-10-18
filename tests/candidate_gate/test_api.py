from __future__ import annotations

import re
from io import BytesIO
from pypdf import PdfReader
from urllib.parse import urlparse

import requests
import websocket


def _json(response: requests.Response):
    assert response.headers.get("content-type", "").startswith("application/json"), response.text
    return response.json()


def _items(payload):
    if isinstance(payload, list):
        return payload
    assert isinstance(payload, dict)
    for key in ("items", "jobs", "data", "results"):
        if isinstance(payload.get(key), list):
            return payload[key]
    raise AssertionError(f"response does not contain an item list: {payload}")


# The candidate is the exact unlicensed release image (Community). Pro features are refused
# with the license error here; their behaviour is proved by the contract suite under a
# substituted license trust root (see docs/TRUSTED_VALIDATION_SCOPE.md).
def _license_refusal(response, feature):
    assert response.status_code == 403, response.text
    body = _json(response)
    assert body["error"]["code"] == "permission_denied", body
    assert f"Feature '{feature}' requires Pro" in body["detail"] and "Current tier: community" in body["detail"], body


def test_candidate_community_refuses_new_orders_but_renders_existing_invoices(operator, viewer, base_url):
    products=_items(_json(operator.get(f'{base_url}/api/products',timeout=10)))
    product=next(row for row in products if row.get('sku')=='ODIN-CANDIDATE-001')
    _license_refusal(operator.post(f'{base_url}/api/orders',json={
        'order_number':'Synthetic 客户 invoice', 'customer_name':'Synthetic José 日本語 한국어',
        'items':[{'product_id':product['id'],'quantity':1,'unit_price':0}],
    },timeout=10), 'orders')
    # Existing records stay readable on Community (keep_existing_records): the seeded order's
    # invoice still renders from the packaged fonts in this exact image.
    order=next(row for row in _items(_json(viewer.get(f'{base_url}/api/orders',timeout=10)))
               if row.get('order_number')=='ODIN-CANDIDATE-ORDER-001')
    assert viewer.get(f'{base_url}/api/orders/{order["id"]}/invoice.pdf',timeout=10).status_code==403
    response=operator.get(f'{base_url}/api/orders/{order["id"]}/invoice.pdf',timeout=15)
    assert response.status_code==200, response.text
    assert response.headers['content-disposition']==f'attachment; filename="invoice_{order["id"]}.pdf"'
    assert response.content.startswith(b'%PDF-')
    reader=PdfReader(BytesIO(response.content))
    text='\n'.join(page.extract_text() for page in reader.pages)
    assert all(value in text for value in ['Candidate Student','Invoice Total:'])
    assert all(value not in text for value in ['Platform Fees','Payment Fees','Cost Analysis','Profit:'])
    fonts={str(font.get_object().get('/BaseFont')) for page in reader.pages
           for font in (page.get('/Resources') or {}).get('/Font', {}).values()}
    assert any('NotoSans' in name for name in fonts), fonts  # the image's packaged fonts (invoice_generator)


def test_candidate_community_refuses_failure_and_time_reports(viewer,base_url):
    _license_refusal(viewer.get(f'{base_url}/api/analytics/failures',timeout=10), 'analytics')
    _license_refusal(viewer.get(f'{base_url}/api/analytics/time-accuracy',timeout=10), 'analytics')


def test_candidate_health_spa_static_setup_and_perimeter(base_url: str, api_key: str, admin):
    live = requests.get(f"{base_url}/health", timeout=10)
    assert live.status_code == 200
    assert _json(live)["status"] == "ok"

    ready = requests.get(f"{base_url}/health/ready", timeout=10)
    assert ready.status_code == 200
    assert _json(ready)["ready"] is True

    shell = requests.get(f"{base_url}/login", timeout=10)
    assert shell.status_code == 200
    assert "text/html" in shell.headers.get("content-type", "")
    asset_match = re.search(r'(?:src|href)="(/assets/[^"]+)"', shell.text)
    assert asset_match, "compiled SPA shell did not reference a built asset"
    asset = requests.get(f"{base_url}{asset_match.group(1)}", timeout=10)
    assert asset.status_code == 200
    assert len(asset.content) > 100

    setup = requests.get(f"{base_url}/api/setup/status", timeout=10)
    assert setup.status_code == 200
    assert _json(setup) == {"needs_setup": False, "has_users": True, "is_complete": False}
    setup_retry = requests.post(
        f"{base_url}/api/setup/admin",
        json={"username": "attacker", "email": "attacker@example.invalid", "password": "AttackerPass-123!"},
        timeout=10,
    )
    assert setup_retry.status_code == 403

    anonymous = requests.get(f"{base_url}/api/printers", timeout=10)
    assert anonymous.status_code == 401
    invalid_key = admin.get(
        f"{base_url}/api/printers", headers={"X-API-Key": f"wrong-{api_key[:4]}"}, timeout=10
    )
    assert invalid_key.status_code == 401


def test_candidate_personas_and_rbac(admin, operator, viewer, base_url: str):
    expected = {
        "admin": "candidate-admin@example.invalid",
        "operator": "candidate-operator@example.invalid",
        "viewer": "candidate-viewer@example.invalid",
    }
    for role, session in (("admin", admin), ("operator", operator), ("viewer", viewer)):
        response = session.get(f"{base_url}/api/auth/me", timeout=10)
        assert response.status_code == 200, response.text
        identity = _json(response)
        assert identity["username"] == expected[role]
        assert identity["role"] == role

    users = admin.get(f"{base_url}/api/users", timeout=10)
    assert users.status_code == 200
    assert {(user["username"], user["role"]) for user in _json(users)} == {
        ("candidate-admin@example.invalid", "admin"),
        ("candidate-operator@example.invalid", "operator"),
        ("candidate-viewer@example.invalid", "viewer"),
    }
    assert operator.get(f"{base_url}/api/users", timeout=10).status_code == 403
    assert viewer.get(f"{base_url}/api/users", timeout=10).status_code == 403
    assert viewer.post(f"{base_url}/api/models", json={"name": "Viewer mutation"}, timeout=10).status_code == 403
    assert operator.put(f"{base_url}/api/config", json={}, timeout=10).status_code == 403


def test_candidate_seeded_domain_graph_and_mutation_round_trip(admin, operator, viewer, base_url: str):
    printers = _items(_json(viewer.get(f"{base_url}/api/printers", timeout=10)))
    printer = next(item for item in printers if item["name"] == "ODIN Candidate Gate Printer")
    assert printer["is_active"] is False
    assert printer["has_api_key"] is False
    assert printer.get("camera_url") in (None, "")

    spools = _items(_json(viewer.get(f"{base_url}/api/spools", timeout=10)))
    assert any(item.get("qr_code") == "ODIN-CANDIDATE-SPOOL" for item in spools)
    models = _items(_json(viewer.get(f"{base_url}/api/models", timeout=10)))
    assert any(item["name"] == "ODIN Candidate Calibration Cube" for item in models)
    products = _items(_json(viewer.get(f"{base_url}/api/products", timeout=10)))
    product = next(item for item in products if item.get("sku") == "ODIN-CANDIDATE-001")
    product_detail = _json(viewer.get(f"{base_url}/api/products/{product['id']}", timeout=10))
    assert product_detail["component_count"] == 1
    assert product_detail["components"][0]["model_name"] == "ODIN Candidate Calibration Cube"
    orders = _items(_json(viewer.get(f"{base_url}/api/orders", timeout=10)))
    order = next(item for item in orders if item.get("order_number") == "ODIN-CANDIDATE-ORDER-001")
    order_detail = _json(viewer.get(f"{base_url}/api/orders/{order['id']}", timeout=10))
    assert order_detail["items"][0]["product_id"] == product["id"]
    jobs = _items(_json(viewer.get(f"{base_url}/api/jobs", timeout=10)))
    candidate_jobs = [item for item in jobs if item["item_name"].startswith("ODIN Candidate Cube")]
    assert {item["status"] for item in candidate_jobs} == {"pending", "completed"}

    stats_response = viewer.get(f"{base_url}/api/stats", timeout=10)
    assert stats_response.status_code == 200, stats_response.text
    stats = _json(stats_response)
    assert stats["printers"] == {"total": 1, "active": 0}
    assert stats["jobs"]["pending"] == 1
    assert stats["jobs"]["scheduled"] == 0
    assert stats["jobs"]["printing"] == 0
    assert stats["models"] == 1

    reordered = operator.post(
        f"{base_url}/api/printers/reorder", json={"printer_ids": [printer["id"]]}, timeout=10
    )
    assert reordered.status_code == 200, reordered.text
    assert _json(reordered) == {"success": True, "order": [printer["id"]]}

    seeded_model = next(item for item in models if item["name"] == "ODIN Candidate Calibration Cube")
    scheduled = operator.post(
        f"{base_url}/api/models/{seeded_model['id']}/schedule", timeout=10
    )
    assert scheduled.status_code == 200, scheduled.text
    scheduled_job = _json(scheduled)
    assert scheduled_job["model_id"] == seeded_model["id"]
    assert scheduled_job["model_name"] == "ODIN Candidate Calibration Cube"
    assert scheduled_job["status"] == "pending"
    visible_jobs = _items(_json(viewer.get(f"{base_url}/api/jobs", timeout=10)))
    assert any(
        job["id"] == scheduled_job["job_id"] and job["status"] == "pending"
        for job in visible_jobs
    )
    removed_job = operator.delete(f"{base_url}/api/jobs/{scheduled_job['job_id']}", timeout=10)
    assert removed_job.status_code == 204, removed_job.text

    created = operator.post(
        f"{base_url}/api/models",
        json={"name": "ODIN Candidate CRUD Model", "build_time_hours": 0.25, "default_filament_type": "PLA"},
        timeout=10,
    )
    assert created.status_code == 201, created.text
    model_id = _json(created)["id"]
    updated = operator.patch(
        f"{base_url}/api/models/{model_id}", json={"notes": "candidate round trip"}, timeout=10
    )
    assert updated.status_code == 200, updated.text
    assert _json(updated)["notes"] == "candidate round trip"
    # models.delete is admin-only in the 2.0 default matrix (core/rbac_policy.py).
    refused = operator.delete(f"{base_url}/api/models/{model_id}", timeout=10)
    assert refused.status_code == 403 and _json(refused)["detail"] == "Insufficient permissions", refused.text
    deleted = admin.delete(f"{base_url}/api/models/{model_id}", timeout=10)
    assert deleted.status_code == 204, deleted.text
    assert viewer.get(f"{base_url}/api/models/{model_id}", timeout=10).status_code == 404

    license_response = requests.get(f"{base_url}/api/license", timeout=10)
    assert license_response.status_code == 200
    license_data = _json(license_response)
    assert license_data["tier"] == "community"
    assert license_data["max_users"] == 1
    over_limit = admin.post(
        f"{base_url}/api/users",
        json={
            "username": "candidate-fourth@example.invalid",
            "email": "candidate-fourth@example.invalid",
            "password": "FourthUserPass-123!",
            "role": "viewer",
        },
        timeout=10,
    )
    assert over_limit.status_code == 403
    assert "limit" in str(_json(over_limit)).lower()


def test_candidate_websocket_uses_purpose_limited_token(admin, base_url: str):
    token_response = admin.post(f"{base_url}/api/auth/ws-token", timeout=10)
    assert token_response.status_code == 200, token_response.text
    ws_token = _json(token_response)["token"]
    parsed = urlparse(base_url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    connection = websocket.create_connection(
        f"{ws_scheme}://{parsed.netloc}/api/v1/ws?token={ws_token}",
        timeout=10,
        origin=base_url,
    )
    try:
        connection.send("ping")
        assert connection.recv() == "pong"
        connection.send('{"type":"subscribe","channels":["events"]}')
        message = connection.recv()
        assert '"type":"subscribed"' in message.replace(" ", "")
    finally:
        connection.close()



def test_candidate_metrics_have_parseable_exposition_and_require_auth(viewer,base_url):
    from prometheus_client.parser import text_string_to_metric_families
    response=viewer.get(f'{base_url}/api/metrics',timeout=10)
    assert response.status_code==200
    assert response.headers['content-type'].startswith('text/plain')
    samples=[s for f in text_string_to_metric_families(response.text) for s in f.samples]
    totals={s.name:s.value for s in samples if not s.labels}
    # The owned candidate seed deliberately disables every synthetic printer.
    assert totals['odin_printers_total']==0 and totals['odin_queue_depth']>=0
    assert totals['odin_alerts_unread']>=0
    assert not any(s.name=='odin_printer_print_count_total' for s in samples)
    assert requests.get(f'{base_url}/api/metrics',timeout=10).status_code==401


def test_candidate_printer_history_remains_readable_and_authenticated(viewer,base_url):
    printer_rows=_items(_json(viewer.get(f'{base_url}/api/printers',timeout=10)))
    assert printer_rows
    identifier=printer_rows[0]['id']
    for endpoint in ['telemetry','hms-history','nozzle-status']:
        response=viewer.get(f'{base_url}/api/printers/{identifier}/{endpoint}',timeout=10)
        assert response.status_code==200
        payload=_json(response)
        if endpoint=='telemetry':assert payload==[]
        if endpoint=='hms-history':assert payload['entries']==[] and payload['total']==0
        assert requests.get(f'{base_url}/api/printers/{identifier}/{endpoint}',timeout=10).status_code==401
        assert viewer.get(f'{base_url}/api/printers/999999/{endpoint}',timeout=10).status_code==404


def test_candidate_generic_upload_and_revision_lifecycle_preserve_real_payload(operator,viewer,base_url):
    payload=b'; Synthetic candidate upload\nG28\n'
    response=operator.post(f'{base_url}/api/print-files/upload',files={'file':('Synthetic candidate revision.gcode',payload,'application/octet-stream')},timeout=15)
    assert response.status_code==200
    body=_json(response);identifier=body['model_id'];file_id=body['id']
    preview=operator.get(f'{base_url}/api/files/{file_id}/preview-model',timeout=10)
    assert preview.status_code==200 and preview.content==payload
    assert viewer.post(f'{base_url}/api/models/{identifier}/revisions',files={'file':('Synthetic denied.gcode',payload,'application/octet-stream')},timeout=10).status_code==403
    revision_payload=b'; Synthetic distinct revision one\nG1 X10\n'
    created=operator.post(f'{base_url}/api/models/{identifier}/revisions',files={'file':('Synthetic revision.gcode',revision_payload,'application/octet-stream')},timeout=15)
    assert created.status_code==200 and _json(created)['revision_number']==1
    second=operator.post(f'{base_url}/api/models/{identifier}/revisions',files={'file':('Synthetic revision two.gcode',b'; Synthetic distinct revision two\nG1 X20\n','application/octet-stream')},timeout=15)
    assert second.status_code==200 and _json(second)['revision_number']==2
    reverted=operator.post(f'{base_url}/api/models/{identifier}/revisions/1/revert',timeout=15)
    assert reverted.status_code==200 and _json(reverted)=={'revision_number':3,'reverted_from':1}
    rows=_json(operator.get(f'{base_url}/api/models/{identifier}/revisions',timeout=10))
    assert [row['revision_number'] for row in rows]==[3,2,1]
    assert rows[0]['changelog']=='Reverted to v1'
    assert len({row['file_path'] for row in rows})==3
    # Revision payload bytes are separately checked against owned SQLite/Pg files.
    # These HTTP routes do not expose revision downloads or select an active payload.


def test_candidate_generic_file_queue_is_atomic_and_preserves_payload(operator,viewer,base_url):
    payload=b'; Synthetic direct file queue\nG1 X12 Y34\n'
    response=operator.post(f'{base_url}/api/print-files/upload',files={'file':('Synthetic direct queue.gcode',payload,'application/octet-stream')},timeout=15)
    assert response.status_code==200
    uploaded=_json(response);file_id=uploaded['id'];model_id=uploaded['model_id']
    detail=viewer.get(f'{base_url}/api/print-files/{file_id}',timeout=10)
    assert detail.status_code==200 and _json(detail)['model_id']==model_id
    assert 'stored_path' not in _json(detail)
    assert viewer.post(f'{base_url}/api/print-files/{file_id}/schedule',timeout=10).status_code==403
    queued=operator.post(f'{base_url}/api/print-files/{file_id}/schedule',timeout=10)
    assert queued.status_code==200
    job_id=_json(queued)['job_id']
    assert _json(operator.get(f'{base_url}/api/print-files/{file_id}',timeout=10))['job_id']==job_id
    job=operator.get(f'{base_url}/api/jobs/{job_id}',timeout=10)
    assert job.status_code==200 and _json(job)['model_id']==model_id and _json(job)['status']=='pending'
    assert operator.post(f'{base_url}/api/print-files/{file_id}/schedule',timeout=10).status_code==409
    assert operator.delete(f'{base_url}/api/print-files/{file_id}',timeout=10).status_code==409
    listed=_json(viewer.get(f'{base_url}/api/print-files?include_scheduled=true&limit=100',timeout=10))
    row=next(row for row in listed if row['id']==file_id)
    assert row['job_id']==job_id and row['job_status']=='pending'
    preview=viewer.get(f'{base_url}/api/files/{file_id}/preview-model',timeout=10)
    assert preview.status_code==200 and preview.content==payload
    # Exact job payload/actor/charge columns and competing queue commits are separately
    # checked against the owned native database. This case never dispatches a printer.
