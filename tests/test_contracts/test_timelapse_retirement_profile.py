"""Probe timing instrumentation preserves call/context/error behavior."""
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from tests.test_contracts.test_timelapse_edit_publication import edit,prepared_source,encoder,frame_writer,review_db,owned_runtime
from ops.database_parity.timelapse_retirement_profile import RetirementProfile

def test_nested_exclusive_and_maxima():
    ticks=iter((0.,1.,3.,5.));p=RetirementProfile(clock=lambda:next(ticks))
    with p.span('parent'):
        with p.span('child'):pass
    v=p.snapshot('owned')['metrics']
    assert v['parent']['inclusive_total']==5 and v['parent']['exclusive_total']==3
    assert v['child']['exclusive_total']==2 and v['child']['inclusive_max']==2

def test_exception_spans_and_patch_restoration():
    p=RetirementProfile();obj=SimpleNamespace(fn=lambda:1);original=obj.fn
    def failure():raise ValueError('owned')
    p.patch(obj,'fn',p.function('owned_failure',failure))
    try:
        with pytest.raises(ValueError):obj.fn()
        assert p.snapshot('owned')['metrics']['owned_failure']['count']==1
    finally:p.close()
    assert obj.fn is original

def test_context_suppression_and_yield_lifetime_excluded():
    p=RetirementProfile();events=[]
    @contextmanager
    def original():
        events.append('enter')
        try:yield 7
        except ValueError:events.append('suppressed')
        finally:events.append('exit')
    with p.context('owned',original)() as value:
        assert value==7;raise ValueError('owned')
    assert events==['enter','suppressed','exit']
    assert set(p.snapshot('owned')['metrics'])=={'owned.enter','owned.exit'}


def test_actual_sql_events_pair_success_failure_and_restore(edit):
    from sqlalchemy import text,event
    from sqlalchemy.exc import OperationalError
    from core.timelapse_media_lock import MediaGuard
    engine=edit[1];p=RetirementProfile();before=MediaGuard.assert_owned
    try:
        p.install(engine)
        with engine.connect() as c:
            assert c.execute(text('SELECT 1')).scalar_one()==1
            with pytest.raises(OperationalError):c.execute(text('SELECT nonexistent_owned_column'))
        assert p.snapshot('owned')['metrics']['sql_execute']['count']==2
        listeners=list(p.listeners)
    finally:p.close()
    assert MediaGuard.assert_owned is before
    assert all(not event.contains(engine,name,fn) for engine,name,fn in listeners)
