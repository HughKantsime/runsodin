"""Caller-owned erasure of identity-bearing ephemeral WebSocket replay rows.

No commit, logging, raw JSON replacement or global queue purge. Delivered/inflight
and future producer writes are outside this persisted snapshot cleanup contract.
"""
import json
import re
from sqlalchemy import text

MAX_ROWS = 4096
MAX_PAYLOAD_BYTES = 262144
MAX_TOTAL_BYTES = 4194304
MAX_NODES = 16384
MAX_DEPTH = 32

class ReplayCleanupUnavailable(RuntimeError):
    pass


def erase_identifying_replay(db, *, user_id, identifiers):
    names = {value for value in identifiers if isinstance(value, str) and value}
    emails = [re.compile(r'(?<![\w@])'+re.escape(value)+r'(?![\w@])', re.IGNORECASE)
              for value in names if '@' in value]
    producer_suffixes = (' submitted a print job', ' resubmitted a previously rejected job')
    total = 0
    rows = db.execute(text('SELECT id, CASE WHEN length(data)<=:limit THEN data ELSE NULL END AS data FROM ws_events ORDER BY id LIMIT :rows').execution_options(stream_results=True), {'limit': MAX_PAYLOAD_BYTES, 'rows': MAX_ROWS+1}).yield_per(16)
    for number, row in enumerate(rows, 1):
        if number > MAX_ROWS or not isinstance(row.data, str):
            raise ReplayCleanupUnavailable()
        size = len(row.data.encode('utf-8'))
        total += size
        if size > MAX_PAYLOAD_BYTES or total > MAX_TOTAL_BYTES:
            raise ReplayCleanupUnavailable()
        try:
            payload = json.loads(row.data)
        except json.JSONDecodeError:
            # Non-replayable rows: only literal/canonical JSON-escaped forms are covered.
            matched = any(value in row.data or json.dumps(value)[1:-1] in row.data
                          for value in names if '@' in value or len(value) >= 8)
        else:
            stack = [(payload, 0, '', None)]
            nodes = 0
            matched = False
            while stack:
                value, depth, field, owner = stack.pop()
                nodes += 1
                if nodes > MAX_NODES or depth > MAX_DEPTH:
                    raise ReplayCleanupUnavailable()
                if isinstance(value, dict):
                    # Examine values, never keys as identity text.
                    actors = [value[key] for key in ('user_id', 'actor_user_id', 'submitted_by', 'charged_to_user_id')
                              if type(value.get(key)) is int]
                    if user_id in actors:
                        matched = True
                        owner = True
                    elif actors:
                        owner = False
                    stack.extend((child, depth+1, key, owner) for key, child in value.items())
                elif isinstance(value, list):
                    stack.extend((child, depth+1, field, owner) for child in value)
                elif isinstance(value, str):
                    if any(pattern.search(value) for pattern in emails):
                        matched = True
                    if owner is not False and field in ('username', 'submitter_username', 'actor_username') and value in names:
                        matched = True
                    # Existing job alert producers place the account's name first.
                    # Common names in arbitrary operational text are not ownership evidence.
                    if owner is not False and field == 'message' and any(value.startswith(name+suffix)
                                                 for name in names for suffix in producer_suffixes):
                        matched = True
        if matched:
            deleted = db.execute(text('DELETE FROM ws_events WHERE id=:id AND data=:data'),
                                 {'id': row.id, 'data': row.data})
            if deleted.rowcount != 1:
                remaining = db.execute(text('SELECT 1 FROM ws_events WHERE id=:id'), {'id': row.id}).first()
                if remaining is not None:
                    raise ReplayCleanupUnavailable()
