"""Owned probe-only numeric profiling. Never records arguments or SQL text."""
import time,threading,sys
from contextlib import contextmanager
from functools import wraps

class RetirementProfile:
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.local=threading.local();self.metrics={};self.boundaries={};self.patches=[];self.listeners=[];self.budget=None
    def _stack(self):
        if not hasattr(self.local,'stack'):self.local.stack=[]
        return self.local.stack
    def begin(self,name):
        token=[name,self.clock(),0.0];self._stack().append(token);return token
    def end(self,token):
        stack=self._stack()
        if not stack or stack[-1] is not token:raise AssertionError('Unpaired owned profile span')
        elapsed=self.clock()-token[1];stack.pop()
        if stack:stack[-1][2]+=elapsed
        exclusive=max(0,elapsed-token[2]);m=self.metrics.setdefault(token[0],{'count':0,'inclusive_total':0.,'exclusive_total':0.,'inclusive_max':0.,'exclusive_max':0.})
        m['count']+=1;m['inclusive_total']+=elapsed;m['exclusive_total']+=exclusive;m['inclusive_max']=max(m['inclusive_max'],elapsed);m['exclusive_max']=max(m['exclusive_max'],exclusive)
    @contextmanager
    def span(self,name):
        token=self.begin(name)
        try:yield
        finally:self.end(token)
    def boundary(self,name):
        if self.budget is None:return
        remaining=self.budget.deadline-self.clock();elapsed=60-remaining
        b=self.boundaries.setdefault(name,{'count':0,'elapsed_min':elapsed,'elapsed_max':elapsed,'remaining_min':remaining,'remaining_max':remaining})
        b['count']+=1;b['elapsed_min']=min(b['elapsed_min'],elapsed);b['elapsed_max']=max(b['elapsed_max'],elapsed);b['remaining_min']=min(b['remaining_min'],remaining);b['remaining_max']=max(b['remaining_max'],remaining)
    def function(self,name,original,*,boundary=False):
        @wraps(original)
        def call(*args,**kwargs):
            if boundary:self.boundary(name+'.before')
            try:
                with self.span(name):return original(*args,**kwargs)
            finally:
                if boundary:self.boundary(name+'.after')
        return call
    def context(self,name,original):
        @wraps(original)
        @contextmanager
        def call(*args,**kwargs):
            cm=original(*args,**kwargs);self.boundary(name+'.enter.before')
            with self.span(name+'.enter'):value=cm.__enter__()
            self.boundary(name+'.enter.after')
            try:yield value
            except BaseException:
                self.boundary(name+'.exit.before')
                with self.span(name+'.exit'):suppress=cm.__exit__(*sys.exc_info())
                self.boundary(name+'.exit.after')
                if not suppress:raise
            else:
                self.boundary(name+'.exit.before')
                with self.span(name+'.exit'):cm.__exit__(None,None,None)
                self.boundary(name+'.exit.after')
        return call
    def patch(self,obj,key,value):
        self.patches.append((obj,key,getattr(obj,key)));setattr(obj,key,value)
    def install(self,engine):
        from sqlalchemy import event
        from sqlalchemy.engine.result import MappingResult
        import core.timelapse_media_lock as guard
        import core.timelapse_publication_validation as validation
        import modules.archives.timelapse_snapshot_graph as graph
        import modules.archives.timelapse_speed_dependencies as speed
        import modules.archives.timelapse_lifetime_action as action
        from core.timelapse_request_budget import VerificationBudget
        for obj,key,name,boundary in ((guard._RootChain,'check','guard_chain',False),(guard.MediaGuard,'_locks_match','guard_locks',False),(guard.MediaGuard,'assert_owned','guard_owned',False),(guard.MediaGuard,'assert_root_identity','guard_identity',False),(graph.SnapshotGraphReadView,'_build','graph_build',False),(graph.SnapshotGraphReadView,'_batch','graph_batch',False),(graph.SnapshotGraphReadView,'execute','graph_execute',False),(speed,'_completed_speed_source','speed_source',False),(action,'_load','load',True),(action,'_sharing','sharing',False),(action,'apply_verified_action','pre_fs_apply',True),(VerificationBudget,'check','budget_check',True)):
            self.patch(obj,key,self.function(name,getattr(obj,key),boundary=boundary))
        frozen=self.function('frozen_operation',validation.operation)
        self.patch(validation,'operation',frozen);self.patch(speed,'operation',frozen)
        self.patch(guard,'_root',self.context('root_reopen',guard._root))
        self.patch(action,'_transaction',self.context('transaction',action._transaction))
        self.patch(action,'verify_action',self.context('verify',action.verify_action))
        init=VerificationBudget.__init__
        @wraps(init)
        def budget_init(value,*args,**kwargs):
            result=init(value,*args,**kwargs);self.budget=value;return result
        self.patch(VerificationBudget,'__init__',budget_init)
        fetch=MappingResult.fetchmany
        @wraps(fetch)
        def fetchmany(value,*args,**kwargs):
            if not any(t[0]=='graph_batch' for t in self._stack()):return fetch(value,*args,**kwargs)
            with self.span('graph_fetch'):return fetch(value,*args,**kwargs)
        self.patch(MappingResult,'fetchmany',fetchmany)
        def before(c,cur,statement,params,context,many):context._odin_owned_numeric_profile=self.begin('sql_execute')
        def after(c,cur,statement,params,context,many):
            token=getattr(context,'_odin_owned_numeric_profile',None)
            if token is not None:
                context._odin_owned_numeric_profile=None;self.end(token)
        def error(context):
            execution=context.execution_context
            if execution is not None:
                token=getattr(execution,'_odin_owned_numeric_profile',None)
                if token is not None:execution._odin_owned_numeric_profile=None;self.end(token)
        for name,fn in (('before_cursor_execute',before),('after_cursor_execute',after),('handle_error',error)):
            event.listen(engine,name,fn);self.listeners.append((engine,name,fn))
    def snapshot(self,label):
        if self._stack():raise AssertionError('Owned profile snapshot inside active span')
        value={'action':label,'metrics':self.metrics,'budget_boundaries':self.boundaries,'scope':'Numeric owned probe only; nested exclusive spans; instrumentation consumes unchanged real60s deadline.'}
        self.metrics={};self.boundaries={};self.budget=None
        return value
    def close(self):
        from sqlalchemy import event
        try:
            for engine,name,fn in reversed(self.listeners):event.remove(engine,name,fn)
        finally:
            for obj,key,value in reversed(self.patches):setattr(obj,key,value)
            self.listeners.clear();self.patches.clear();self.budget=None
