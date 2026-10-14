"""Listener setup transaction ends before application connection use."""
from types import SimpleNamespace
import pytest
from sqlalchemy.engine import make_url
from core import database_config as config

@pytest.mark.parametrize('failure',[None,'acquire','commit'])
def test_restore_listener_commits_only_successful_setup_and_refuses_failure(monkeypatch,tmp_path,failure):
    monkeypatch.setenv("ODIN_DATA_DIR",str(tmp_path))
    callbacks=[];calls=[]
    monkeypatch.setattr(config,'resolve_database_url',lambda *a,**kw:make_url('postgresql+psycopg://synthetic@db/owned'))
    monkeypatch.setattr(config,'create_engine',lambda *a,**kw:SimpleNamespace())
    def register(*a,**kw):
        def decorate(fn):callbacks.append(fn);return fn
        return decorate
    monkeypatch.setattr(config.event,'listens_for',register)
    engine=config.create_database_engine('synthetic',role='api')
    assert engine._odin_restore_barrier_required is True and len(callbacks)==2
    class Cursor:
        def execute(self,statement,*args):
            calls.append(statement)
            if failure=='acquire' and 'pg_advisory' in statement:raise RuntimeError('Synthetic setup failure')
        def close(self):calls.append('cursor-close')
    class Connection:
        def close(self):calls.append('connection-close')
        def cursor(self):return Cursor()
        def commit(self):
            calls.append('commit')
            if failure=='commit':raise RuntimeError('Synthetic commit failure')
    if failure:
        with pytest.raises(RuntimeError,match='Synthetic'):callbacks[0](Connection(),SimpleNamespace(invalidate=lambda *args:None))
    else:callbacks[0](Connection(),SimpleNamespace(invalidate=lambda *args:None))
    assert calls[-1]==('connection-close' if failure else 'cursor-close')
    if failure:assert calls[-2]=='cursor-close'
    if failure=='acquire':assert 'commit' not in calls
    elif failure=='commit':assert calls[-4:]==['SET lock_timeout = 0','commit','cursor-close','connection-close']
    else:assert calls[-3:]==['SET lock_timeout = 0','commit','cursor-close']
