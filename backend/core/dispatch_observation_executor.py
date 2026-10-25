"""Text-only transaction view over native SQLAlchemy and daemon DBAPI callers.

Never owns, commits or closes a transaction. Parameters use SQLAlchemy's compiler,
not string substitution, and the daemon adapter performs dialect translation.
"""
from types import SimpleNamespace
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy import text

class _Row(tuple):
    def __new__(cls,values,names):
        row=super().__new__(cls,values)
        row._mapping=dict(zip(names,values))
        return row

class _Result:
    def __init__(self,cursor):
        self._cursor=cursor
        self.rowcount=cursor.rowcount
        self._names=[column[0] for column in cursor.description] if cursor.description else []
        self._mapping=False
    def mappings(self):
        self._mapping=True
        return self
    def fetchone(self):
        value=self._cursor.fetchone()
        if value is None:return None
        row=_Row(value,self._names)
        return row._mapping if self._mapping else row
    first=fetchone
    def fetchall(self):
        return list(self)
    all=fetchall
    def __iter__(self):
        while True:
            row=self.fetchone()
            if row is None:return
            yield row
    def one(self):
        row=self.fetchone()
        if row is None or self.fetchone() is not None:raise ValueError('Expected one observation authority row')
        return row
    def scalar_one(self):
        return self.one()[0]

class TransactionView:
    def __init__(self,executor):
        self.executor=executor
        self.native=isinstance(executor,(Connection,Session))
        self.dialect=(executor.get_bind().dialect if isinstance(executor,Session) else executor.dialect) if self.native else SimpleNamespace(name='postgresql' if getattr(executor,'_postgres',False) else 'sqlite')
    def execute(self,statement,parameters=None):
        if self.native:return self.executor.execute(statement,parameters or {})
        if not hasattr(statement,'compile'):statement=text(statement)
        compiled=statement.compile(dialect=dialect(paramstyle='qmark'))
        values=compiled.construct_params(parameters or {})
        return _Result(self.executor.cursor().execute(str(compiled),tuple(values[name] for name in compiled.positiontup)))
