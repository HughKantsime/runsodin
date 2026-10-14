"""Internal physical staging only; no unlink, final metadata delete or API wiring."""
from dataclasses import dataclass
from sqlalchemy.engine import Engine
from core.timelapse_manifest import _root
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_quarantine import quarantine_directory
from core.timelapse_staging_fs import StageFilesystem,StagingFilesystemUnavailable
from modules.archives.timelapse_retirement import _floor,RetirementUnavailable
from modules.archives.timelapse_stage_transactions import (load_stage_plan,commit_stage_step,
    directory_identity,StageTransactionUnavailable)


class StagingUnavailable(RuntimeError):
    def __init__(self,reason='file_io'):
        self.reason=reason
        super().__init__('Media staging could not be confirmed')


@dataclass(frozen=True)
class StagedRetirement:
    operation_id:str
    state:str='staged'


def stage_retirement(engine,root,timelapse_id,principal):
    if not isinstance(engine,Engine) or type(timelapse_id) is not int or timelapse_id<=0:raise StagingUnavailable('unknown_authority')
    try:
        _floor(principal)
        with acquire_media_guard(root,timelapse_id) as guard:
            with _root(root) as (root_fd,root_identity):
                guard.assert_root_identity(root_identity)
                plan=load_stage_plan(engine,timelapse_id,principal,root_identity)
                quarantine_identity=directory_identity('.retirement',plan.quarantine_row)
                parent_identities={path:directory_identity(path,row) for path,row in plan.parent_rows.items()}
                with quarantine_directory(root,root_identity,quarantine_identity) as (quarantine_fd,_):
                    filesystem=StageFilesystem(root_fd,quarantine_fd,root_identity,quarantine_identity,plan.objects,parent_identities,plan.move_rows,guard)
                    for row in plan.objects:
                        if row['state']=='originally_absent':filesystem.verify(row)
                    if plan.record['state']!='in_flight':
                        filesystem.admit();commit_stage_step(engine,timelapse_id,principal,plan,deadline=filesystem.deadline)
                    present=[row for row in plan.objects if row['state']!='originally_absent']
                    ordered=sorted(present,key=lambda row:(row['kind']=='directory',-row['relative_path'].count('/'),row['object_index']))
                    for row in ordered:
                        filesystem.admit()
                        phase=plan.move_rows[row['object_index']]['phase']
                        if phase=='staged':filesystem.verify(row);continue
                        if phase=='planned':
                            commit_stage_step(engine,timelapse_id,principal,plan,row,phase='rename_pending',deadline=filesystem.deadline)
                        else:
                            # Fresh authorization and revision admission for recovery too.
                            commit_stage_step(engine,timelapse_id,principal,plan,row,phase='rename_pending',deadline=filesystem.deadline)
                        filesystem.admit()
                        stamp=filesystem.stage(row)
                        filesystem.admit();commit_stage_step(engine,timelapse_id,principal,plan,row,phase='staged',stamp=stamp,deadline=filesystem.deadline)
                    for row in plan.objects:filesystem.verify(row)
                    filesystem.admit()
                    # A final current-authority/revision check precedes a staged receipt.
                    commit_stage_step(engine,timelapse_id,principal,plan,deadline=filesystem.deadline)
                    return StagedRetirement(plan.operation)
    except StagingUnavailable:raise
    except (RetirementUnavailable,StagingFilesystemUnavailable,StageTransactionUnavailable) as error:raise StagingUnavailable(error.reason) from None
    except Exception:raise StagingUnavailable('file_io') from None
