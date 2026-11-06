"""Bounded-memory JSONL export with detached Ed25519 digest-manifest signature."""
import base64
import hashlib
import json
import tempfile
import zipfile
from datetime import timedelta
from sqlalchemy import text
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from core.audit import audit_download
from core.audit_integrity import verify
from modules.system.audit_siem import payload


def export(db, date_from, date_to, bundle=False):
    if date_to < date_from:
        raise HTTPException(422, 'date_to must be on or after date_from')
    if date_to.year == 9999:
        raise HTTPException(422, 'date_to is outside the supported range')
    output = tempfile.TemporaryFile(mode='w+b')
    try:
        from core.audit_snapshot import snapshot
        from core.audit_integrity import canonical_projection
        with snapshot(db.get_bind()) as connection:
            result = verify(connection)
            if result.get('status') == 'audit_key_missing':
                raise HTTPException(409, 'audit_key_missing: recover the original keyring first')
            if any(not b['known'] for b in result['breaks']):
                raise HTTPException(409, 'Audit integrity verification failed')
            digest, count = hashlib.sha256(), 0
            rows = connection.execute(text(f'''SELECT {canonical_projection(connection)} FROM audit_logs a WHERE chain_seq>0 AND timestamp>=:start AND timestamp<:end
                ORDER BY chain_seq''').execution_options(stream_results=True, max_row_buffer=256), {'start': date_from.isoformat(), 'end': (date_to+timedelta(days=1)).isoformat()})
            for row in rows.mappings():
                line = payload(row)+b'\n'
                output.write(line)
                digest.update(line)
                count += 1
        manifest = {'protocol': 'odin-audit-export-v1', 'sha256': digest.hexdigest(), 'rows': count,
                    'date_from': date_from.isoformat(), 'date_to': date_to.isoformat(),
                    'known_breaks': result['breaks'], 'chain_head': result['head'], 'genesis': result['genesis'], 'chain_rows': result['rows']}
        from core.audit_signing import existing_device_key
        key, public, installation_id, fingerprint = existing_device_key()
        manifest.update(installation_id=installation_id, key_fingerprint=fingerprint)
        encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
        signature = base64.b64encode(key.sign(encoded)).decode()
        audit_download(db, 'export.downloaded', details={'export': 'audit-jsonl', 'manifest': manifest})
        output.seek(0)
        if bundle:
            archive = tempfile.TemporaryFile(mode='w+b')
            try:
                with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as zipped:
                    with zipped.open('audit-logs.jsonl', 'w', force_zip64=True) as content:
                        while chunk := output.read(65536):
                            content.write(chunk)
                    zipped.writestr('manifest.json', encoded)
                    zipped.writestr('signature.ed25519.b64', signature)
                    zipped.writestr('public-key.ed25519.b64', public)
                archive.seek(0)
            except BaseException:
                archive.close()
                raise
            output.close()
            output = archive
    except BaseException:
        output.close()
        raise

    def stream():
        try:
            while chunk := output.read(65536):
                yield chunk
        finally:
            output.close()
    return StreamingResponse(stream(), media_type='application/zip' if bundle else 'application/x-ndjson', headers={
        'Content-Disposition': 'attachment; filename="audit-logs.'+('zip' if bundle else 'jsonl')+'"',
        'X-ODIN-Audit-Manifest': base64.b64encode(encoded).decode(),
        'X-ODIN-Audit-Signature': signature, 'X-ODIN-Audit-Public-Key': public,
        'X-ODIN-Audit-Chain-Head': result['head'], 'Cache-Control': 'no-store'})
