"""Run every single-call-site mutant against the entire controlled suite.

Small source snapshots stay in the temporary evidence directory and are deleted on exit.
One unchanged full-suite baseline is required. JUnit assertion failures kill a
mutant; collection/setup errors never count. Every result is retained even if
another mutant survives. No -x selection or single-test shortcut is used.
"""
import concurrent.futures
import shutil
import json
import os
from pathlib import Path
import subprocess
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PYTHON = '/Users/ollama/actions-runner-runsodin/_work/runsodin/runsodin/.venv-contracts/bin/python'
MUTANTS = [
    ('tombstone-policy', 'backend/core/controlled_parts.py', "if not db.execute(text('SELECT 1 FROM jobs WHERE id=:id'), {'id': identifier}).first():", 'if False:'),
    ('tombstone-denied-union', 'backend/core/controlled_redaction.py', "UNION SELECT c.job_id FROM controlled_job_tombstones c WHERE ' + membership + ' AND NOT EXISTS (SELECT 1 FROM jobs WHERE id=c.job_id)", 'UNION SELECT c.job_id FROM controlled_job_tombstones c WHERE 1=0'),
    ('overlay-active-camera', 'backend/core/controlled_overlay.py', "allowed(db, 'printer', identifier, None)", "allowed(db, 'printer_names', identifier, None)"),
    ('printer-card-name', 'backend/core/display_names.py', "if printer.current_print_display_name and not allowed(db, 'printer_names', printer.id, user):", 'if False:'),
    ('reprint-controlled-only', 'backend/core/controlled_reprint.py', "AND NOT (' + hidden + ')'", "AND (1=1 OR ' + hidden + ')'"),

    ('preview-organization', 'backend/modules/organizations/routes_controlled_retention.py', 'if check_org_access(actor, r[1]) and allowed', 'if allowed'),
    ('unmarked-stream-fast-path', 'backend/modules/printers/controlled_camera.py', '        if enabled:', '        if True:'),
    ('source-replacement', 'backend/core/controlled_request.py', '        if source_change and marked:', '        if False:'),
    ('json-content-types', 'backend/core/controlled_request.py', "    return message.get_content_maintype() == 'application' and (", '    return False and ('),
    ('education-marking', 'backend/core/controlled_marking.py', "    if linked or db.execute(owner_query, {'files': files}).first():", '    if False:'),
    ('path', 'backend/core/controlled_request.py', "            if owner is not None and (check_org_access(actor, owner[0]) or kind == 'printer' and owner[1] in (True, 1)):\n                assert_access(db, kind, identifier, actor)\n", "            if owner is not None and (check_org_access(actor, owner[0]) or kind == 'printer' and owner[1] in (True, 1)):\n                pass\n"),
    ('nested-body', 'backend/core/controlled_request.py', '        for value in body.values():\n            if isinstance(value, (dict, list)):\n                authorize_references(db, actor, value)\n', ''),
    ('mjpeg', 'backend/modules/printers/controlled_camera.py', "        assert_access(db, 'printer', printer_id,\n                      {'id': viewer['user_id'], '_auth_kind': viewer.get('auth_kind')})", '        return'),
    ('websocket', 'backend/core/controlled_events.py', '    return not (unknown and any_marks(db)) and all(allowed(db, kind, identifier, principal) for kind, identifier in refs)', '    return True'),
    ('alerts', 'backend/core/recipient_policy.py', "and allowed(db, 'job', job_id, dict(u)) and allowed(db, 'printer', printer_id, dict(u))", 'and True'),
    ('retention-max', 'backend/modules/organizations/routes_controlled_parts.py', 'Field(ge=1, le=3650)', 'Field(ge=1)'),
    ('active-claim', 'backend/modules/organizations/routes_controlled_parts.py', "AND state IN ('preparing','adapter_entered','accepted','uncertain')", 'AND 1=0'),
    ('external-copy', 'backend/core/controlled_events.py', '    if (unknown and any_marks(db)) or any(policies(db, kind, identifier) for kind, identifier in refs):', '    if False:'),
    ('backup-acknowledgement', 'backend/core/controlled_exports.py', '    if marked and acknowledged is not True:', '    if False:'),
    ('retention-mark-clock', 'backend/core/controlled_policy_clock.py', "base = max(completed, as_utc_datetime(rule['marked_at']))", 'base = completed'),
    ('retention-preview', 'backend/core/controlled_retention.py', "    return bool(rules) and all(r['purge_previewed_at'] for r in rules)", '    return True'),
    ('list-jobs-callsite', 'backend/modules/jobs/routes/jobs_crud.py', '    query = query.filter(text(controlled)).params(**controlled_params)', '    query = query'),
    ('dispatch-precheck', 'backend/modules/printers/dispatch.py', '        reason = dispatch_refusal(controlled_db, job_id, printer_id)', '        reason = None'),
    ('admission-recheck', 'backend/modules/printers/dispatch_admission.py', "            controlled = dispatch_refusal(conn, job['id'], job['printer_id'])", '            controlled = None'),
    ('retention-min-policies', 'backend/core/controlled_retention.py', 'return min(deadlines) <=', 'return max(deadlines) <='),
    ('inactive-member-runtime', 'backend/core/controlled_parts.py', 'WHERE u.id=:id AND m.audience_id=:group AND u.is_active IS TRUE', 'WHERE u.id=:id AND m.audience_id=:group'),
    ('inactive-member-sql', 'backend/core/controlled_parts.py', 'WHERE cu.id=:_controlled_user AND cu.is_active IS TRUE AND cam.audience_id=cp.audience_id', 'WHERE cu.id=:_controlled_user AND cam.audience_id=cp.audience_id'),
    ('audience-sql', 'backend/core/controlled_parts.py', 'AND cam.audience_id=cp.audience_id', ''),
    ('backup-superadmin', 'backend/core/controlled_exports.py', "    if marked and (not actor or actor.get('role') != 'admin' or actor.get('group_id') is not None):", '    if False:'),
    ('response-middleware', 'backend/core/app.py', '    app.middleware("http")(controlled_response)', '    pass'),
    ('get-job-callsite', 'backend/modules/jobs/routes/jobs_crud.py', "    assert_access(db, 'job', job_id, current_user)", '    pass'),
    ('mjpeg-relay-hook', 'backend/modules/printers/mjpeg_relay.py', '                            await run_in_threadpool(check_frame, viewer)', '                            pass'),
    ('webrtc-admission', 'backend/modules/printers/camera_routes.py', "    if policies(db, 'printer', printer_id):\n        refuse", '    if False:\n        refuse'),
    ('webrtc-recheck', 'backend/modules/printers/camera_routes.py', "        if policies(db, 'printer', printer_id):\n            refuse", '        if False:\n            refuse'),
    ('audit-csv-callsite', 'backend/modules/reporting/routes/exports.py', '    logs = redact_audit_rows(db, current_user, logs)\n\n    output', '    logs = logs\n\n    output'),
    ('audit-list-callsite', 'backend/modules/reporting/routes/exports.py', '    logs = redact_audit_rows(db, current_user, logs)\n\n    return {', '    logs = logs\n\n    return {'),
    ('finished-printer-policy', 'backend/core/controlled_parts.py', 'WHERE printer_id=:id ORDER BY started_at DESC,id DESC LIMIT 1', 'WHERE printer_id=:id AND 1=0 ORDER BY started_at DESC,id DESC LIMIT 1'),
    ('finished-printer-websocket', 'backend/core/controlled_events.py', "printer_status(db, data['printer_id'], principal, data)", 'data'),
    ('finished-printer-status', 'backend/modules/printers/routes_status.py', 'return printer_status(db, printer_id, current_user, _fetch_printer_live_status(printer_id, db))', 'return _fetch_printer_live_status(printer_id, db)'),
    ('finished-printer-list', 'backend/modules/printers/routes_crud.py', 'printer_status(db, printer.id, current_user, _fetch_printer_live_status(printer.id, db))', '_fetch_printer_live_status(printer.id, db)'),
    ('gdpr-submitted', 'backend/core/controlled_redaction.py', "'jobs_submitted': 'job'", "'jobs_submitted': None"),
    ('gdpr-charged', 'backend/core/controlled_redaction.py', "'jobs_charged': 'job'", "'jobs_charged': None"),
    ('gdpr-archives', 'backend/core/controlled_redaction.py', "'print_archives': 'archive'", "'print_archives': None"),
    ('gdpr-uploads', 'backend/core/controlled_redaction.py', "'uploaded_print_files': 'file'", "'uploaded_print_files': None"),
    ('conflict-name', 'backend/modules/jobs/routes/jobs_lifecycle.py', "if allowed(db, 'job', conflict.id, current_user)", 'if True'),
    ('order-product-sku', 'backend/core/controlled_redaction.py', "'sku', 'product_sku', 'description'", "'sku', 'description'"),
    ('invoice-render', 'backend/modules/orders/routes/orders_crud.py', 'invoice = redact_export(db, current_user, enriched.model_dump())', 'invoice = enriched.model_dump()'),
    ('log-viewer', 'backend/modules/system/routes_admin.py', '    check_log_access(db, user, acknowledge_controlled)\n    log_path = _LOG_FILES.get(source)\n    if not log_path or not os.path.isfile(log_path):\n        return', '    log_path = _LOG_FILES.get(source)\n    if not log_path or not os.path.isfile(log_path):\n        return'),
    ('log-stream', 'backend/modules/system/routes_admin.py', '    check_log_access(db, user, acknowledge_controlled, audit=False)', '    pass'),
    ('log-stream-recheck', 'backend/modules/system/routes_admin.py', '                stream_log_access(db.get_bind(), user, acknowledge_controlled, token=token)', '                pass'),
    ('alert-read-copy', 'backend/modules/notifications/routes/alerts.py', "data = AlertResponse.model_validate(redact_export(db, current_user, data.model_dump(), resource_kind='alert'))", 'data = data'),
    ('deleted-job-audit', 'backend/modules/jobs/routes/jobs_crud.py', '    redact_deleted_job_history(db, job.id)', '    pass'),
    ('mqtt-cross-process-cache', 'backend/core/controlled_mqtt.py', '>= 1:', '>= 30:'),
    ('middleware-response-eligibility', 'backend/core/middleware/controlled_response.py', "    if (not 200 <= response.status_code < 300 or response.status_code == 204\n            or request.method == 'HEAD'\n            or response.headers.get('content-type', '').split(';')[0] != 'application/json'):", '    if response.status_code >= 400:'),
    ('component-identity', 'backend/core/controlled_redaction.py', "'components': 'component'", "'components': 'model'"),
    ('timelapse-job-identity', 'backend/core/controlled_redaction.py', "if context == 'timelapse' and key == 'print_job_id':", 'if False:'),
    ('audience-retention-clock', 'backend/core/controlled_policy_clock.py', "marked = before['marked_at'] if before else", "marked = now.isoformat() if before else"),

    ('bulk-deleted-job-audit', 'backend/modules/jobs/bulk_deletion.py', '    redact_job_history_ids(db, {jid: restricted[jid] for jid in removed if jid in restricted})', '    pass'),
    ('retention-deleted-job-audit', 'backend/modules/system/routes_config.py', '                redact_job_history_ids(db, {jid: restricted[jid] for jid in removed_ids if jid in restricted})', '                pass'),
    ('org-deleted-job-audit', 'backend/modules/jobs/services.py', '    redact_job_history_ids(db, restricted)', '    pass'),
]


def suite(directory=ROOT):
    return [str(p.relative_to(directory)) for p in sorted((directory/'tests/test_contracts').glob('test_controlled*.py'))] + ['tests/test_contracts/test_post027_migration_054.py', 'tests/test_contracts/test_e3_deletion_paths.py']



def pytest(directory, name, evidence, scratch):
    env = {**os.environ, 'ODIN_CONTAINER_EXEC': '/usr/bin/false',
           'PYTHONPATH': str(directory/'backend')+os.pathsep+str(directory), 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPYCACHEPREFIX':str(scratch/'bytecode')}
    report = evidence/(name+'.xml')
    with (evidence/(name+'.log')).open('w') as stream:
        result = subprocess.run([PYTHON, '-m', 'pytest', *suite(directory), '-q', '--disable-warnings', '--tb=short',
                                 '--junitxml='+str(report), '--basetemp='+str(scratch/'pytest-temp')], cwd=directory, env=env,
                                stdout=stream, stderr=subprocess.STDOUT, timeout=600)
    cases = ET.parse(report).findall('.//testcase')
    failures = [c for c in cases if c.find('failure') is not None]
    errors = [c for c in cases if c.find('error') is not None]
    skipped = [c for c in cases if c.find('skipped') is not None]
    assertions = [c for c in failures if any(v in (c.find('failure').text or '') for v in ('AssertionError', 'assert ', 'DID NOT RAISE'))]
    return {'exit': result.returncode, 'passed': len(cases)-len(failures)-len(errors)-len(skipped),
            'total':len(cases), 'failed': len(failures), 'errors':len(errors), 'skipped':len(skipped),
            'assertion_failures':len(assertions), 'failed_tests':[c.get('classname')+'::'+c.get('name') for c in failures]}


def snapshot(destination):
    for folder in ('backend', 'tests', 'ops'):
        shutil.copytree(ROOT/folder, destination/folder,
                        ignore=shutil.ignore_patterns('__pycache__', '*.onnx', '.pytest_cache'))
    for name in ('pytest.ini', 'pyproject.toml', 'VERSION'):
        if (ROOT/name).exists():
            shutil.copy(ROOT/name, destination/name)


def run(selected=None):
    evidence = Path(tempfile.mkdtemp(prefix='odin-controlled-mutations-'))
    evidence.mkdir(parents=True, exist_ok=True)
    # About 95 MiB per snapshot, excluding weights/bytecode, in the temporary evidence directory.
    # Three workers plus baseline bound source copies below 400 MiB. Every
    # scratch directory is removed on exit; this checkout is never mutated.
    with tempfile.TemporaryDirectory(prefix='.mutation-temp-', dir=evidence) as directory:
        scratch = Path(directory)
        frozen = scratch/'baseline'
        frozen.mkdir()
        snapshot(frozen)
        baseline = pytest(frozen, 'baseline', evidence, frozen)
        (evidence/'baseline.json').write_text(json.dumps(baseline,indent=2)+'\n')
        assert baseline['exit'] == 0, f'Full controlled baseline failed: {baseline}'
        def probe(mutant):
            name, filename, old, new = mutant
            with tempfile.TemporaryDirectory(prefix=name+'-', dir=scratch) as directory:
                work = Path(directory)
                for folder in ('backend', 'tests', 'ops'):
                    shutil.copytree(frozen/folder, work/folder)
                for config in ('pytest.ini', 'pyproject.toml', 'VERSION'):
                    if (frozen/config).exists():
                        shutil.copy(frozen/config, work/config)
                target = work/filename
                source = target.read_text()
                assert source.count(old) == 1, f'{name}: expected one call site, found {source.count(old)}'
                target.write_text(source.replace(old,new,1))
                result = pytest(work, name, evidence, work)
            result.update(guard=name, killed=result['exit']==1 and result['errors']==0
                          and result['assertion_failures']>0 and result['total']==baseline['total'])
            return result
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(probe,m):m[0] for m in MUTANTS if not selected or m[0] in selected}
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                except Exception as error:
                    result = {'guard':futures[future], 'killed':False, 'error':str(error)}
                results.append(result)
                print(result['guard'], 'KILLED' if result['killed'] else 'SURVIVED/INVALID', result, flush=True)
                (evidence/'results.json').write_text(json.dumps(results,indent=2)+'\n')
        assert all(r['killed'] for r in results), 'Some mutants survived or were invalid; see results.json'
        return results


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mutant', action='append', choices=[m[0] for m in MUTANTS])
    run(parser.parse_args().mutant)
