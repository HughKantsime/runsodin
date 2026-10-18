"""The single-pass usage report is byte-identical to the pre-rewrite oracle.

tests/test_contracts/_education_usage_oracle.py is the implementation as it stood
before the rewrite. Both run against the same randomized inputs, with time and the
viewer's currency frozen, and their JSON must match byte for byte (key order, int
vs float, None) -- or both must raise the same exception.

Two input layers:
* synthetic result rows fed through a stub session: every value shape the row
  arithmetic must survive (refunds, mixed units, missing identity, malformed
  timestamps and filament JSON, overflow, duplicate ids);
* a real bootstrapped SQLite schema seeded per seed across tenants, cost centers,
  statuses, economic identities and window-boundary dates, exercising the SQL the
  rewrite changed (the scoped select no longer re-evaluates the ownership proof).
"""
import json
import math
import random
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from tests.test_contracts import _education_usage_oracle as oracle
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 18, 12, 0, 0, 123456, tzinfo=timezone.utc)
STATUSES = ['completed', 'failed', 'submitted', 'pending', 'scheduled', 'printing',
            'cancelled', 'rejected', 'COMPLETED', None, 'unknown-status']


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


def _product():
    from modules.reporting import education_usage
    return education_usage


@pytest.fixture
def frozen(monkeypatch):
    """Freeze both implementations' clock and the viewer currency read."""
    from core import finance_currency
    product = _product()
    monkeypatch.setattr(product, 'datetime', _FrozenDateTime)
    monkeypatch.setattr(oracle, 'datetime', _FrozenDateTime)
    currencies = {}
    monkeypatch.setattr(finance_currency, 'read_currency',
                        lambda db, owner_scope, owner_key: currencies.get(owner_key))
    return currencies


def _outcome(function, days, db, viewer):
    try:
        return 'ok', json.dumps(function(days, db, viewer), default=repr, allow_nan=True)
    except Exception as exc:  # the oracle's failure is part of the contract
        detail = getattr(exc, 'detail', None)
        return 'raised', f'{type(exc).__name__}:{getattr(exc, "status_code", None)}:{detail if detail is not None else exc}'


def _assert_equivalent(days, db, viewer):
    expected = _outcome(oracle.build_education_usage_report, days, db, viewer)
    actual = _outcome(_product().build_education_usage_report, days, db, viewer)
    assert actual == expected, (viewer, days)
    return expected


# ---------------------------------------------------------------- synthetic rows

def _timestamp(rng, base):
    choice = rng.randrange(9)
    moment = base + timedelta(hours=rng.uniform(-30, 30))
    if choice == 0:
        return None
    if choice == 1:
        return moment.replace(tzinfo=None).isoformat(' ')
    if choice == 2:
        return moment.replace(tzinfo=None)
    if choice == 3:
        return moment.astimezone(timezone(timedelta(hours=rng.choice([-7, 2, 5.5])))).isoformat()
    if choice == 4:
        return 'not-a-timestamp'
    if choice == 5:
        return 12345  # wrong type
    return moment.replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S')


def _number(rng, positive=True):
    return rng.choice([None, 0, -1, -12.5, 1, 2, 3, 0.5, 2.25, 7.125, 1e308, 1e-9, True, False,
                       '3', float('nan'), float('inf'), -float('inf')]
                      + ([rng.uniform(0.1, 20) for _ in range(6)] if positive else []))


def _requirements(rng):
    choice = rng.randrange(14)
    grams = lambda: rng.choice([rng.uniform(1, 400), rng.randrange(1, 900), 0, -3, None, '12', 1e308, True])
    if choice == 0:
        return None
    if choice == 1:
        return '{}'
    if choice == 2:
        return 'not json'
    if choice == 3:
        return '[1, 2]'
    if choice == 4:
        return '[' * 5000 + ']' * 5000  # deep JSON
    if choice == 5:
        return '"' + 'x' * 70000 + '"'  # over the size bound
    if choice == 6:
        return {'black': {'grams': grams()}}  # PostgreSQL JSON shape
    if choice == 7:
        return json.dumps({'black': 'grams'})
    if choice == 8:
        return json.dumps({'a': {'grams': grams()}, 'b': {'g': 3}})
    if choice == 9:  # a valid object exactly at, or one past, the 65536-character bound
        body = json.dumps({'black': {'grams': 5, 'pad': ''}})
        return body.replace('""', '"' + 'p' * (65536 - len(body) + rng.choice([0, 1])) + '"')
    return json.dumps({f'slot{n}': {'grams': grams()} for n in range(rng.randrange(1, 4))})


def _synthetic_rows(rng, count):
    users = {n: (f'user-{n}', f'user-{n}@example.test', rng.choice(['viewer', 'operator', 'admin']))
             for n in range(1, rng.randrange(2, 9))}
    rows, identifier = [], 0
    for _ in range(count):
        identifier += rng.choice([1, 1, 1, 2, 17])
        submitter = rng.choice(list(users))
        username, email, role = users[submitter]
        if rng.random() < 0.05:
            username = 'renamed-later'  # first row of a user names its bucket
        base = NOW - timedelta(days=rng.uniform(0, 95))
        printer = rng.choice([None, 1, 2, 3, 4, 9])
        model = rng.choice([None, 10, 11, 12, 13])
        rows.append({
            'id': identifier, 'status': rng.choice(STATUSES),
            'approved_by': rng.choice([None, None, 3, 5]),
            'rejected_reason': rng.choice([None, None, None, 'too big', '']),
            'quantity': _number(rng), 'duration_hours': _number(rng),
            'estimated_cost': rng.choice([_number(rng), -rng.uniform(1, 50), rng.uniform(0, 80)]),
            'actual_start': _timestamp(rng, base), 'actual_end': _timestamp(rng, base + timedelta(hours=2)),
            'scheduled_start': _timestamp(rng, base), 'scheduled_end': _timestamp(rng, base + timedelta(hours=3)),
            'created_at': rng.choice([base.replace(tzinfo=None).isoformat(' '), base,
                                      base.replace(tzinfo=None),
                                      base.astimezone(timezone(timedelta(hours=-5))).isoformat()]),
            'submitted_by': submitter, 'printer_id': printer, 'model_id': model,
            'username': username, 'email': email, 'role': role,
            'model_hours': _number(rng), 'color_requirements': _requirements(rng),
            'model_name': None if model is None or rng.random() < 0.1 else f'model-{model}',
            'model_org': rng.choice([None, 1, 2, 1.0, True, [1]]),  # [1]: unhashable driver value
            'printer_name': None if printer is None or rng.random() < 0.1 else f'printer-{printer}',
            'printer_org': rng.choice([None, 1, 2, [2]]), 'printer_shared': rng.choice([None, 0, 1, True, False]),
            'owner_proved': rng.choice([1, 1, 1, 0, True, None]),
        })
    if count and rng.random() < 0.03:
        rows.append(dict(rows[0]))  # duplicate identity
    if count and rng.random() < 0.03:
        rows[-1]['created_at'] = 'garbage'  # unparseable submission time
    return rows


class _StubSession:
    """Returns the synthetic result rows for the report query, as SQLAlchemy mappings would."""

    def __init__(self, rows):
        self.rows = rows

    def execute(self, statement, params=None):
        rows = self.rows
        return type('Result', (), {'mappings': lambda _self: type('Mappings', (), {'all': lambda _m: list(rows)})()})()


VIEWERS = [
    {'id': 3, 'role': 'admin', 'group_id': None},   # platform
    {'id': 3, 'role': 'admin', 'group_id': 1},
    {'id': 1, 'role': 'viewer', 'group_id': 1},
    {'id': 5, 'role': 'operator', 'group_id': 2},
    {'id': 6, 'role': 'viewer', 'group_id': None},  # no tenant: empty report
    {'id': 7, 'role': 'viewer', 'group_id': '1'},   # non-integer tenant: empty report
    {'id': 8, 'role': 'viewer', 'group_id': 0},
]


@pytest.mark.parametrize('seed', range(120))
def test_synthetic_rows_match_the_oracle(frozen, seed):
    rng = random.Random(seed)
    frozen.update({1: rng.choice([None, 'USD', 'EUR']), 2: rng.choice([None, 'GBP'])})
    count = rng.choice([0, 1, 2, 5, 40, 150, 400])
    session = _StubSession(_synthetic_rows(rng, count))
    for viewer in VIEWERS:
        _assert_equivalent(rng.choice([7, 30, 90]), session, viewer)


def test_synthetic_corpus_reaches_every_branch(frozen):
    """Guard against a vacuous corpus: the seeds above produce non-empty reports with
    known and unknown facts, overflow, refunds and failures of both implementations."""
    seen = set()
    for seed in range(120):
        rng = random.Random(seed)
        frozen.update({1: None, 2: None})
        rng.choice([None, 'USD', 'EUR']), rng.choice([None, 'GBP'])
        count = rng.choice([0, 1, 2, 5, 40, 150, 400])
        session = _StubSession(_synthetic_rows(rng, count))
        status, body = _outcome(_product().build_education_usage_report, 30, session, VIEWERS[0])
        if status == 'raised':
            seen.add(body.split(':')[0])
            continue
        summary = json.loads(body)['summary']
        for key in ('unknown_cost_jobs', 'unknown_accuracy_jobs', 'unknown_filament_estimate_jobs',
                    'unknown_estimated_hours_jobs', 'unproven_history_jobs', 'accuracy_samples'):
            if summary[key]:
                seen.add(key)
        if summary['total_cost'] is not None and summary['total_cost'] < 0:
            seen.add('net_refund')
        if any(k.endswith('_unavailable_reason') for k in summary):
            seen.add('overflow')
    assert {'HTTPException', 'ValueError', 'unknown_cost_jobs', 'unknown_accuracy_jobs',
            'unknown_filament_estimate_jobs', 'unknown_estimated_hours_jobs', 'unproven_history_jobs',
            'accuracy_samples', 'overflow'} <= seen, seen


# ------------------------------------------------------------------- real schema

def _seed_database(db, rng):
    from core.economic_identity import _add_created
    from modules.jobs.models import Job
    db.execute(text("INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) VALUES "
                    "(6,'student-b','student-b@example.test','fixture','viewer',1,2),"
                    "(7,'operator-a','operator-a@example.test','fixture','operator',1,1)"))
    db.execute(text("INSERT INTO education_cost_centers (id,org_id,name_key,code_key,display_name,code,state,revision,created_by) "
                    "VALUES (8,2,'art','art','Art','ART','active',1,5),(9,1,'retired','old','Retired','OLD','archived',1,3)"))
    db.execute(text("INSERT INTO printers (id,name,org_id,shared) VALUES "
                    "(31,'Own A',1,0),(32,'Foreign B',2,0),(33,'Shared B',2,1),(34,'Unscoped',NULL,0)"))
    def stored(value):  # SQLite stores JSON as text
        return json.dumps(value) if isinstance(value, dict) else value

    models = []
    for identifier, org in ((41, 1), (42, 2), (43, None), (44, 1)):
        db.execute(text("INSERT INTO models (id,name,org_id,build_time_hours,color_requirements) "
                        "VALUES (:id,:name,:org,:hours,:req)"),
                   {'id': identifier, 'name': f'Model {identifier}', 'org': org,
                    'hours': rng.choice([None, 0, 1.5, 4, 1e308]), 'req': stored(_requirements(rng)) if rng.random() < .8 else None})
        models.append(identifier)
    cutoffs = [NOW - timedelta(days=days) for days in (7, 30, 90)]
    for index in range(rng.randrange(25, 70)):
        org = rng.choice([1, 1, 2, None])
        submitter = rng.choice({1: [1, 2, 3, 7], 2: [5, 6], None: [1, 3, 5]}[org])
        status = rng.choice(['completed', 'completed', 'failed', 'submitted', 'pending', 'cancelled', 'rejected', 'printing'])
        created = rng.choice([
            NOW - timedelta(days=rng.uniform(0, 95)),
            rng.choice(cutoffs),                                  # exactly on a window edge
            rng.choice(cutoffs) - timedelta(microseconds=1),      # just outside
            rng.choice(cutoffs) + timedelta(microseconds=1),      # just inside
            NOW, NOW + timedelta(seconds=5),                     # as_of is exclusive; future rows
        ])
        start = created + timedelta(hours=rng.uniform(0, 4))
        hours = rng.choice([None, 0, 0.25, 1, 2.5, 6])
        values = dict(
            item_name=f'Seed {index}', status=status, submitted_by=submitter, charged_to_org_id=org,
            quantity=rng.choice([1, 1, 2, 3, 0, -1]), duration_hours=rng.choice([None, 0.5, 2, 1e308]),
            estimated_cost=rng.choice([None, 0, 3.5, 12, -4.25]),  # negative: refund/credit estimate
            printer_id=rng.choice([None, 31, 32, 33, 34]), model_id=rng.choice([None] + models),
            approved_by=rng.choice([None, 3]), rejected_reason='policy' if status == 'rejected' else None,
            actual_start=start.replace(tzinfo=None) if hours else None,
            actual_end=(start + timedelta(hours=hours)).replace(tzinfo=None) if hours else None,
            scheduled_start=start.replace(tzinfo=None) if rng.random() < .8 else None,
            scheduled_end=(start + timedelta(hours=rng.choice([0.5, 1, 3]))).replace(tzinfo=None) if rng.random() < .8 else None,
        )
        if org is not None and rng.random() < 0.7:
            job = _add_created(db, Job(**values), 'job', owner_org_id=org)  # retained economic identity
        elif org is None and rng.random() < 0.3:
            job = _add_created(db, Job(**values), 'job', owner_org_id=None, global_authority=True)
        else:
            job = Job(**values)  # legacy: no economic identity
            db.add(job)
        db.flush()
        db.execute(text('UPDATE jobs SET created_at=:created WHERE id=:id'), {'created': created, 'id': job.id})
        if org is not None and rng.random() < 0.35:
            try:
                with db.begin_nested():
                    db.execute(text("INSERT INTO education_submissions (org_id,job_id,model_id,cost_center_id,submitted_by,status) "
                                    "VALUES (:org,:job,:model,:center,:user,:status)"),
                               {'org': org, 'job': job.id, 'model': job.model_id,
                                'center': {1: rng.choice([7, 9]), 2: 8}[org], 'user': submitter,
                                'status': rng.choice(['submitted', 'approved', 'rejected'])})
            except Exception:
                pass  # schema refused this combination; the corpus is still deterministic per seed
    db.commit()


@pytest.mark.parametrize('seed', range(12))
def test_seeded_schema_matches_the_oracle(review_db, frozen, seed):  # noqa: F811
    rng = random.Random(1000 + seed)
    frozen.update({1: rng.choice([None, 'USD']), 2: rng.choice([None, 'EUR'])})
    _seed_database(review_db, rng)
    reports = {}
    for viewer in VIEWERS:
        for days in (7, 30, 90):
            reports[(viewer['id'], viewer['group_id'], days)] = _assert_equivalent(days, review_db, viewer)
            review_db.rollback()
    platform = json.loads(reports[(3, None, 90)][1])['summary']
    scoped = json.loads(reports[(3, 1, 90)][1])['summary']
    # Non-vacuous: proved scoped rows, and unproven platform history, both reached the arithmetic.
    assert scoped['total_jobs'] > 0 and platform['total_jobs'] > scoped['total_jobs']


# ------------------------------------------------------------------ CPU budget

def test_serial_report_cpu_budget(frozen):
    """Pin the row arithmetic. 400 proved rows, mostly completed, each joining the summary,
    user, printer and model buckets: the rewrite derives a row's facts once instead of once
    per bucket, so its serial CPU p50 must stay well under the oracle's, measured interleaved
    in one process so machine load affects both alike, and under an absolute ceiling."""
    rng = random.Random(7)
    rows = _synthetic_rows(rng, 400)
    for identifier, row in enumerate(rows, 1):
        row.update(id=identifier, owner_proved=1, created_at=(NOW - timedelta(hours=identifier)).isoformat(),
                   status='completed' if identifier % 5 else 'failed')
    session = _StubSession(rows)
    viewer = {'id': 3, 'role': 'admin', 'group_id': 1}
    samples = {'product': [], 'oracle': []}
    for _ in range(41):
        for name, function in (('product', _product().build_education_usage_report),
                               ('oracle', oracle.build_education_usage_report)):
            started = time.process_time()
            function(30, session, viewer)
            samples[name].append(time.process_time() - started)
    product, reference = (sorted(samples[name])[20] for name in ('product', 'oracle'))
    assert product <= reference * 0.6, (product, reference)
    assert product < 0.015, product  # CPU seconds, serial, 400 rows x 4 buckets
