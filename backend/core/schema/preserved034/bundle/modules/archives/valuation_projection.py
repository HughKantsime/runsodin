"""Closed historical-input parser, never an authorization or cost calculator."""
import json
import re
from datetime import datetime, timezone
from uuid import UUID
from core.finance_currency import SUPPORTED_CURRENCIES
from core.spool_purchase_validation import canonical_decimal, MAX_REVISION

MAX_SNAPSHOT_BYTES = 65536
_NONCE = re.compile(r'[0-9a-f]{32}', re.ASCII)
_UTC = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.(?:\d{3}|\d{6})Z', re.ASCII)
_TOP = {'version','state','reason','attempt_uid','attempt_birth','captured_at','target','sources'}
_TARGET = {'owner_scope','owner_key','job_uid','item_uid','order_uid','currency'}
_SOURCE = {'spool_uid','birth_nonce','state','reason'}
_EVIDENCE = {'evidence_nonce','revision','currency','price_decimal','net_weight_g_decimal','weight_basis','provenance'}
_REASONS = {None,'missing_commerce_binding','unattested_company_origin','unresolved_material_mapping','invalid_bounded_sources','unresolved_source_valuation'}
_SOURCE_REASONS = {None,'unproved_source_lifetime','foreign_source_owner','missing_purchase_evidence','missing_target_currency','currency_mismatch'}


def _fail():
    raise ValueError('Invalid private valuation snapshot')


def _uuid(value, nullable=False):
    if value is None and nullable:
        return
    if type(value) is not str or len(value) != 36:
        _fail()
    try:
        if str(UUID(value)) != value:
            _fail()
    except (ValueError, AttributeError):
        _fail()


def _nonce(value, nullable=False):
    if value is None and nullable:
        return
    if type(value) is not str or _NONCE.fullmatch(value) is None:
        _fail()


def _object(pairs):
    if len(pairs) > 32 or len({key for key, _ in pairs}) != len(pairs):
        _fail()
    return dict(pairs)


def _preparse(payload):
    if type(payload) is not str:
        _fail()
    try:
        if len(payload.encode('utf-8')) > MAX_SNAPSHOT_BYTES:
            _fail()
    except UnicodeError:
        _fail()
    depth = 0
    quoted = escaped = False
    for char in payload:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '{[':
            depth += 1
            if depth > 8:
                _fail()
        elif char in '}]':
            depth -= 1
            if depth < 0:
                _fail()
    if depth or quoted:
        _fail()


def parse_private_valuation(payload):
    """Only call after independent owner proof and server-side byte admission."""
    _preparse(payload)
    try:
        snapshot = json.loads(payload, object_pairs_hook=_object, parse_constant=lambda _: _fail())
    except (ValueError, TypeError, RecursionError):
        _fail()
    if type(snapshot) is not dict or type(snapshot.get('version')) is not int or snapshot['version'] != 1:
        _fail()
    value = snapshot.get('unit_valuation')
    if type(value) is not dict or set(value) != _TOP or type(value['version']) is not int or value['version'] != 2 or value['state'] not in ('known','unknown') or not (value['reason'] is None or type(value['reason']) is str) or value['reason'] not in _REASONS:
        _fail()
    _uuid(value['attempt_uid'], nullable=True)
    _nonce(value['attempt_birth'])
    clock = value['captured_at']
    if type(clock) is not str or _UTC.fullmatch(clock) is None:
        _fail()
    try:
        if datetime.fromisoformat(clock).utcoffset() != timezone.utc.utcoffset(None):
            _fail()
    except ValueError:
        _fail()
    target = value['target']
    if target is not None:
        if type(target) is not dict or set(target) != _TARGET or type(target['owner_key']) is not int:
            _fail()
        if not ((target['owner_scope'] == 'global' and target['owner_key'] == 0) or (target['owner_scope'] == 'org' and target['owner_key'] > 0)):
            _fail()
        for field in ('job_uid','item_uid','order_uid'):
            _uuid(target[field])
        if target['currency'] is not None and target['currency'] not in SUPPORTED_CURRENCIES:
            _fail()
    elif value['state'] != 'unknown' or value['reason'] not in ('missing_commerce_binding','unattested_company_origin'):
        _fail()
    sources = value['sources']
    if type(sources) is not list or len(sources) > 16:
        _fail()
    seen = set()
    for source in sources:
        if type(source) is not dict or not _SOURCE <= set(source) or set(source) - (_SOURCE | _EVIDENCE) or source['state'] not in ('known','unknown') or not (source['reason'] is None or type(source['reason']) is str) or source['reason'] not in _SOURCE_REASONS:
            _fail()
        _uuid(source['spool_uid'], nullable=True)
        _nonce(source['birth_nonce'], nullable=True)
        if source['spool_uid'] is not None:
            if source['spool_uid'] in seen:
                _fail()
            seen.add(source['spool_uid'])
        evidence = set(source) & _EVIDENCE
        if evidence:
            if evidence != _EVIDENCE or target is None:
                _fail()
            _uuid(source['spool_uid']); _nonce(source['birth_nonce']); _nonce(source['evidence_nonce'])
            if type(source['revision']) is not int or not 1 <= source['revision'] <= MAX_REVISION:
                _fail()
            if source['currency'] not in SUPPORTED_CURRENCIES or not canonical_decimal(source['price_decimal']) or not canonical_decimal(source['net_weight_g_decimal'], positive=True):
                _fail()
            if source['weight_basis'] != 'purchased_net_filament_g' or source['provenance'] != 'owner_attestation':
                _fail()
            expected_reason = 'missing_target_currency' if target['currency'] is None else ('currency_mismatch' if target['currency'] != source['currency'] else None)
            if source['reason'] != expected_reason or source['state'] != ('known' if expected_reason is None else 'unknown'):
                _fail()
        elif source['state'] != 'unknown' or source['reason'] not in ('unproved_source_lifetime','foreign_source_owner','missing_purchase_evidence'):
            _fail()
        elif source['reason'] in ('foreign_source_owner','missing_purchase_evidence'):
            _uuid(source['spool_uid']); _nonce(source['birth_nonce'])
    if target is None and sources:
        _fail()
    if target is not None:
        _uuid(value['attempt_uid'])
    if sources:
        complete = all(source['state'] == 'known' for source in sources)
        if value['state'] != ('known' if complete else 'unknown') or value['reason'] != (None if complete else 'unresolved_source_valuation'):
            _fail()
    elif target is not None and (value['state'] != 'unknown' or value['reason'] not in ('unresolved_material_mapping','invalid_bounded_sources')):
        _fail()
    if value['state'] == 'known' and (target is None or not sources or value['reason'] is not None or any(source['state'] != 'known' for source in sources)):
        _fail()
    return value


def historical_extension_absent(payload):
    """Valid bounded legacy envelope only; malformed present data never absent."""
    _preparse(payload)
    try:
        envelope=json.loads(payload,object_pairs_hook=_object,parse_constant=lambda _: _fail())
    except (ValueError,TypeError,RecursionError):
        _fail()
    if type(envelope) is not dict or type(envelope.get('version')) is not int or envelope['version']!=1:
        _fail()
    return 'unit_valuation' not in envelope
