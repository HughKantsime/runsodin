"""Exact staged-object unlink/sync primitive; caller owns durable admissions.

A returned result proves namespace absence after directory fsync, not freed
bytes or database completion. This cannot be activated before publisher fencing.
"""
from contextlib import contextmanager
import os
import time
from core.timelapse_manifest import _parts, _facts, _stable, MAX_OBJECTS
from core.timelapse_digest import MAX_BYTES
from core.timelapse_rename import _component
from core.timelapse_staging_fs import StageFilesystem, StagingFilesystemUnavailable


class DrainFilesystemUnavailable(StagingFilesystemUnavailable):
    pass


def _refuse(reason='object_changed'):
    raise DrainFilesystemUnavailable(reason) from None


class DrainFilesystem:
    # These helpers are phase-independent descriptor/content checks.
    admit = StageFilesystem.admit
    entry = StageFilesystem.entry
    identity = StageFilesystem.identity
    stable_directory = StageFilesystem.stable_directory
    check_file = StageFilesystem.check_file
    hash_file = StageFilesystem.hash_file
    check_directory = StageFilesystem.check_directory

    def __init__(self,root_fd,quarantine_fd,root_identity,quarantine_identity,plan,parents,moves,unlinks,guard):
        self.root_fd=root_fd; self.quarantine_fd=quarantine_fd
        self.root=root_identity; self.quarantine=quarantine_identity; self.guard=guard
        self.plan=plan; self.parents=parents; self.moves=moves; self.unlinks=unlinks
        self.deadline=time.monotonic()+60; self.remaining=MAX_BYTES
        by_path={row['relative_path']:row for row in plan}
        if not 1<=len(plan)<=MAX_OBJECTS or len(by_path)!=len(plan):_refuse('phase_uncertain')
        states={'unlink_pending':'staged','unlink_confirmed':'unlink_confirmed',
                'fsync_confirmed':'fsync_confirmed','retired':'retired'}
        present=[]
        if len({row['object_index'] for row in plan})!=len(plan):_refuse('phase_uncertain')
        operation=plan[0]['operation_id']
        for row in plan:
            _parts(row['relative_path'])
            if row['operation_id']!=operation or type(row['object_index']) is not int or row['object_index']<0:_refuse('phase_uncertain')
            index=row['object_index']; receipt=unlinks.get(index)
            if row['state']=='originally_absent':
                if index in moves or receipt is not None:_refuse('phase_uncertain')
                continue
            _component(row['quarantine_name'])
            if row['quarantine_name']!=operation+'_'+str(index):_refuse('phase_uncertain')
            if receipt is not None and (receipt['operation_id']!=operation or receipt['object_index']!=index):_refuse('phase_uncertain')
            move=moves.get(index)
            if move is not None and (move['operation_id']!=operation or move['object_index']!=index or move['kind']!=row['kind']):_refuse('phase_uncertain')
            if move is None or move['phase']!='staged' or type(move['verified_ctime_ns']) is not int or move['verified_ctime_ns']<0:_refuse('phase_uncertain')
            expected='staged' if receipt is None else states.get(receipt['phase'])
            if expected is None or row['state']!=expected:_refuse('phase_uncertain')
            present.append(row)
        if set(unlinks)-{row['object_index'] for row in present}:_refuse('phase_uncertain')
        if sum(row['byte_count'] for row in present if row['kind']=='file')>MAX_BYTES:_refuse('manifest_limit')
        self.original_roots=[row for row in plan if row['relative_path'].rpartition('/')[0] not in by_path]
        if any(row['state']=='originally_absent' and row not in self.original_roots for row in plan):_refuse('phase_uncertain')

    @contextmanager
    def external_directory(self,path):
        if not path:
            self.admit(); yield self.root_fd; self.admit(); return
        parts=_parts(path); parent_path='/'.join(parts[:-1]); name=parts[-1]
        expected=self.parents.get(path)
        if expected is None:_refuse('phase_uncertain')
        with self.external_directory(parent_path) as parent:
            value=self.entry(parent,name,path); self.stable_directory(value,expected)
            descriptor=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            try:
                self.stable_directory(_facts(path,os.fstat(descriptor)),expected)
                yield descriptor
                self.stable_directory(_facts(path,os.fstat(descriptor)),expected)
                self.stable_directory(self.entry(parent,name,path),expected)
            finally:os.close(descriptor)

    def originals_absent(self):
        for row in self.original_roots:
            parts=_parts(row['relative_path'])
            with self.external_directory('/'.join(parts[:-1])) as parent:
                if self.entry(parent,parts[-1],row['relative_path']) is not None:_refuse()

    def destination(self,row,*,hash_content=False):
        value=self.entry(self.quarantine_fd,row['quarantine_name'],row['relative_path'])
        receipt=self.unlinks.get(row['object_index'])
        if value is None:
            if receipt is None:_refuse('phase_uncertain')
            return None
        if receipt is not None and receipt['phase']!='unlink_pending':_refuse()
        stamp=self.moves[row['object_index']]['verified_ctime_ns']
        if row['kind']=='file':
            if hash_content:return self.hash_file(self.quarantine_fd,row['quarantine_name'],row,ignore_ctime=True,verified=stamp)
            self.check_file(value,row,ignore_ctime=True,verified=stamp)
            return value
        if row['kind']=='directory':
            return self.check_directory(self.quarantine_fd,row['quarantine_name'],row,verified=stamp)
        _refuse('phase_uncertain')

    def preflight(self):
        self.originals_absent()
        for row in self.plan:
            if row['state']!='originally_absent':self.destination(row)
        self.admit()

    def unlink_and_sync(self,row):
        receipt=self.unlinks.get(row['object_index'])
        if receipt is None or receipt['phase']!='unlink_pending' or row['state']!='staged':_refuse('phase_uncertain')
        self.originals_absent()
        value=self.destination(row,hash_content=True)
        if value is not None:
            self.admit()
            # Last exact pathname check before the irreversible syscall.
            if self.entry(self.quarantine_fd,row['quarantine_name'],row['relative_path'])!=value:_refuse()
            if row['kind']=='file':os.unlink(row['quarantine_name'],dir_fd=self.quarantine_fd)
            else:os.rmdir(row['quarantine_name'],dir_fd=self.quarantine_fd)
        self.assert_absent(row)
        self.admit(); os.fsync(self.quarantine_fd); self.admit()
        self.assert_absent(row)
        return 'absence_synced'

    def assert_absent(self,row):
        self.originals_absent()
        if self.entry(self.quarantine_fd,row['quarantine_name'],row['relative_path']) is not None:_refuse()
