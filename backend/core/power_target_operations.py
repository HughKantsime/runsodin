"""Private 027 transaction primitives, not command/authorization permits.

The later activation layer must validate genuine authority, live target ownership,
confirmed durable commits and actual owned worker/release evidence. Nothing here
starts a process, sends a command, commits or adopts an uncertain operation.
"""
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
from uuid import uuid4
from core import power_target_operation_schema as schema
from core.schema.migrator import MigrationError


class PowerStorageError(RuntimeError): pass


@dataclass(frozen=True)
class PowerFacts:
    printer_id: int
    target_key: str
    config_key: str
    destination_key: str
    purpose: str
    physical_job_id: int | None = None
    scheduled_job_id: int | None = None
    observation_key: str | None = None
    request_nonce: str | None = None
    actor_user_id: int | None = None
    auth_kind: str | None = None
    authority_key: str | None = None


_SEAL = object()


@dataclass(frozen=True)
class _StorageOwner:
    operation_nonce: str
    owner_nonce: str = field(repr=False)
    process_id: int = field(repr=False)
    seal: object = field(repr=False, compare=False)


def _history(executor):
    from importlib import import_module
    migration = import_module('core.schema.migrations.027_power_target_operations')
    migration._source()
    path = Path(migration.__file__)
    wanted = (hashlib.sha256(path.read_bytes()).hexdigest(), schema.dialect(executor))
    row = schema.one(executor,'SELECT checksum,dialect FROM odin_schema_migrations WHERE migration_id=?',(schema.MIGRATION_ID,))
    if row is None or tuple(row) != wanted: raise PowerStorageError('Recorded power storage required')
    schema.validate(executor)


def _valid(facts):
    if type(facts) is not PowerFacts: raise PowerStorageError('Closed power facts required')
    if facts.printer_id is None or any(getattr(facts,n) is None for n in ('target_key','config_key','destination_key')): raise PowerStorageError('Required original power facts missing')
    for value in (facts.printer_id,facts.physical_job_id,facts.scheduled_job_id,facts.actor_user_id):
        if value is not None and (type(value) is not int or not 0<value<=schema.MAX): raise PowerStorageError('Invalid original power ID')
    for value,width in ((facts.target_key,64),(facts.config_key,64),(facts.destination_key,64),(facts.observation_key,64),(facts.authority_key,64),(facts.request_nonce,32)):
        if value is not None and (type(value) is not str or re.fullmatch('[0-9a-f]{'+str(width)+'}',value) is None): raise PowerStorageError('Invalid power equality key')
    automatic = facts.purpose in ('observed_on','cooldown_off')
    manual = facts.purpose in ('manual_on','manual_off','manual_toggle')
    if not automatic and not manual: raise PowerStorageError('Unsupported power purpose')
    if automatic:
        if facts.physical_job_id is None or facts.observation_key is None or any(v is not None for v in (facts.request_nonce,facts.actor_user_id,facts.auth_kind,facts.authority_key)): raise PowerStorageError('Automatic observation facts required')
    else:
        if any(v is not None for v in (facts.physical_job_id,facts.scheduled_job_id,facts.observation_key)) or any(v is None for v in (facts.request_nonce,facts.actor_user_id,facts.auth_kind,facts.authority_key)) or facts.auth_kind not in ('session_jwt','bearer_jwt','scoped_api_key','legacy_global_api_key'): raise PowerStorageError('Manual original request facts required')


def _lookup_replay(executor, facts):
    """Storage comparison only; caller must revalidate selected credential first."""
    _valid(facts); _history(executor)
    if facts.request_nonce is None: return None
    names = tuple(PowerFacts.__dataclass_fields__)
    row = schema.one(executor,'SELECT '+','.join(names)+',state,outcome FROM '+schema.TABLE+' WHERE request_nonce=?',(facts.request_nonce,))
    if row is None: return None
    if tuple(row[:len(names)]) != tuple(getattr(facts,n) for n in names): raise PowerStorageError('Power request nonce conflicts')
    if row[-2] != 'complete': raise PowerStorageError('Original power operation is not completed')
    return row[-1]  # Retained reported outcome; no current observation/cache/command.


def _stage_operation(executor, facts):
    _valid(facts); _history(executor)
    if facts.request_nonce is not None and schema.one(executor,'SELECT 1 FROM '+schema.TABLE+' WHERE request_nonce=?',(facts.request_nonce,)):
        raise PowerStorageError('Power request nonce already retained')
    operation = uuid4().hex; owner = uuid4().hex
    names = ('operation_nonce','owner_nonce',*PowerFacts.__dataclass_fields__,'state','revision')
    values = (operation,owner,*(getattr(facts,n) for n in PowerFacts.__dataclass_fields__),'preparing',0)
    schema.execute(executor,'INSERT INTO '+schema.TABLE+'('+','.join(names)+') VALUES('+','.join('?' for _ in names)+')',values)
    return _StorageOwner(operation,owner,os.getpid(),_SEAL)


def _stage_transition(executor, owner, expected_revision, expected_state, state, *, outcome=None, reason=None):
    """Stage storage CAS. This private marker is NOT an OS quiescence receipt."""
    if type(owner) is not _StorageOwner or owner.seal is not _SEAL or owner.process_id!=os.getpid(): raise PowerStorageError('Original process storage ownership required')
    if type(expected_revision) is not int or not 0<=expected_revision<schema.MAX: raise PowerStorageError('Invalid power revision')
    allowed = {'preparing':('command_started','release_pending','cleanup_uncertain'),'command_started':('release_pending','cleanup_uncertain'),'release_pending':('complete','cleanup_uncertain')}
    if state not in allowed.get(expected_state,()): raise PowerStorageError('Unsupported power transition')
    if state=='release_pending' and outcome not in ('on_observed','off_observed','not_sent'): raise PowerStorageError('Exact retained outcome required')
    if state!='release_pending' and outcome is not None: raise PowerStorageError('Outcome assignment differs')
    reasons = ('commit_uncertain','start_uncertain','command_uncertain','worker_uncertain','ipc_uncertain','target_uncertain','authority_drift','storage_unavailable')
    if (state=='cleanup_uncertain' and reason not in reasons) or (state!='cleanup_uncertain' and reason is not None): raise PowerStorageError('Exact uncertainty reason required')
    _history(executor)
    timestamp = schema.one(executor,'SELECT pg_catalog.clock_timestamp()' if schema.dialect(executor)=='postgresql' else "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')")[0]
    assignments = ['state=?','revision=revision+1','changed_at=?']; values=[state,timestamp]
    phase = {'command_started':'command_started_at','release_pending':'release_pending_at','complete':'completed_at'}.get(state)
    if phase: assignments.append(phase+'=?'); values.append(timestamp)
    if state=='release_pending': assignments.append('outcome=?'); values.append(outcome)
    if state=='cleanup_uncertain': assignments.append('cleanup_reason=?'); values.append(reason)
    result = schema.execute(executor,'UPDATE '+schema.TABLE+' SET '+','.join(assignments)+' WHERE operation_nonce=? AND owner_nonce=? AND revision=? AND state=?',(*values,owner.operation_nonce,owner.owner_nonce,expected_revision,expected_state))
    if result.rowcount!=1: raise PowerStorageError('Original power CAS changed')
    return expected_revision+1
