import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { profilesApi } from '../../api'
import { Modal } from '../../components/ui'
import ConfirmModal from '../../components/shared/ConfirmModal'

interface Props { profile: {id:number;name:string}; canEdit:boolean; onClose:()=>void }

export default function ProfileHistory({profile,canEdit,onClose}:Props) {
  const cache=useQueryClient()
  const [draft,setDraft]=useState<string|null>(null)
  const [note,setNote]=useState('')
  const [compare,setCompare]=useState<number|null>(null)
  const [restore,setRestore]=useState<number|null>(null)
  const [message,setMessage]=useState('')
  const [failure,setFailure]=useState('')
  const current=useQuery({queryKey:['profile-content',profile.id],queryFn:()=>profilesApi.get(profile.id)})
  const history=useQuery({queryKey:['profile-history',profile.id],queryFn:()=>profilesApi.revisions(profile.id)})
  const rows=history.data||[]
  const latest=rows[0]?.revision_number
  const diff=useQuery({queryKey:['profile-diff',profile.id,compare,latest],queryFn:()=>profilesApi.diffRevisions(profile.id,compare!,latest!),enabled:compare!==null&&latest!==undefined})
  useEffect(()=>{if(draft===null&&current.isSuccess&&!current.isFetching&&typeof current.data?.raw_content==='string')setDraft(current.data.raw_content)},[current.data,current.isSuccess,current.isFetching,draft])
  async function refresh() {
    await Promise.all([
      cache.invalidateQueries({queryKey:['profile-content',profile.id]}),
      cache.invalidateQueries({queryKey:['profile-history',profile.id]}),
      cache.invalidateQueries({queryKey:['profiles']}),
    ])
  }
  const save=useMutation({mutationFn:()=>profilesApi.createRevision(profile.id,draft!,note),onSuccess:async result=>{
    await refresh();setNote('');setCompare(null);setMessage(`Revision ${result.revision_number} saved`);setFailure('')
  },onError:(error:Error)=>{setFailure(error.message||'Unable to save revision');setMessage('')}})
  const revert=useMutation({mutationFn:(number:number)=>profilesApi.revertRevision(profile.id,number),onSuccess:async result=>{
    await refresh();setDraft(null);setNote('');setCompare(null);setMessage(`Revision ${result.revision_number} saved from restored content`);setFailure('')
  },onError:(error:Error)=>{setFailure(error.message||'Unable to restore revision');setMessage('')}})
  const busy=save.isPending||revert.isPending
  const ready=!current.isPending&&!history.isPending&&!current.isError&&!history.isError&&draft!==null
  const changed=draft!==current.data?.raw_content
  const button='px-3 py-2 rounded-md border border-[var(--brand-card-border)] text-sm disabled:opacity-50'
  return <>
    <Modal isOpen onClose={()=>{if(!busy)onClose()}} title={`History: ${profile.name}`} size="xl" mobileSheet={false}>
      <p className="text-sm text-[var(--brand-muted)] mb-4">Saved content versions. Restoring creates a new revision and does not send printer commands.</p>
      {current.isPending||history.isPending ? <p role="status">Loading profile history...</p> : current.isError||history.isError ? <div role="alert">
        <p>Unable to load profile history</p><button className={button} onClick={()=>{current.refetch();history.refetch()}}>Retry history</button>
      </div> : <>
        <label className="block text-sm mb-2" htmlFor="profile-history-content">Current profile content</label>
        <textarea id="profile-history-content" value={draft??''} readOnly={!canEdit} disabled={busy} onChange={event=>{setDraft(event.target.value);setMessage('')}} rows={8} className="w-full min-w-0 rounded-md bg-[var(--brand-input-bg)] p-3 text-sm font-mono border border-[var(--brand-card-border)]" />
        {canEdit&&<div className="mt-3">
          <label className="block text-sm mb-1" htmlFor="profile-history-note">Change notes</label>
          <input id="profile-history-note" value={note} maxLength={4000} disabled={busy} onChange={event=>setNote(event.target.value)} className="w-full rounded-md bg-[var(--brand-input-bg)] p-2 border border-[var(--brand-card-border)]" />
          <button className={`${button} mt-3`} disabled={!ready||!changed||busy} onClick={()=>{setFailure('');save.mutate()}}>{save.isPending?'Saving revision...':'Save revision'}</button>
        </div>}
        {message&&<p role="status" className="mt-3">{message}</p>}
        {failure&&<p role="alert" className="mt-3">{failure}</p>}
        <h3 className="font-medium mt-6 mb-3">Revision history</h3>
        {rows.length===0?<p className="text-sm text-[var(--brand-muted)]">No saved revisions yet. The first edit preserves the original content.</p>:<ol className="space-y-3">
          {rows.map(row=><li key={row.id} className="p-3 rounded-md border border-[var(--brand-card-border)] break-words">
            <p className="font-medium">Revision {row.revision_number}</p><p className="text-sm text-[var(--brand-muted)]">{row.created_at||'Date unavailable'} · {row.created_by===null?'Author unavailable':`User ${row.created_by}`}</p>
            {row.changelog&&<p className="text-sm my-2 whitespace-pre-wrap">{row.changelog}</p>}
            <div className="flex flex-wrap gap-2 mt-2">
              <button className={button} disabled={row.revision_number===latest||busy} onClick={()=>setCompare(row.revision_number)}>Compare revision {row.revision_number}</button>
              {canEdit&&<button className={button} disabled={row.revision_number===latest||busy} onClick={()=>setRestore(row.revision_number)}>Restore revision {row.revision_number}</button>}
            </div>
          </li>)}
        </ol>}
        {compare!==null&&<section className="mt-4" aria-label="Revision comparison">
          <h3 className="font-medium">Revision {compare} compared with {latest}</h3>
          {diff.isPending?<p role="status">Loading comparison...</p>:diff.isError?<div role="alert"><p>Unable to compare revisions</p><button className={button} onClick={()=>diff.refetch()}>Retry comparison</button></div>:<pre className="max-h-64 overflow-auto rounded-md bg-[var(--brand-input-bg)] p-3 text-xs">{diff.data?.diff||'No content differences'}</pre>}
        </section>}
      </>}
    </Modal>
    <ConfirmModal open={restore!==null} title="Restore profile content" message={`Restore revision ${restore}? Your current saved content stays in history. Unpublished edits in this panel will be replaced.`} confirmText="Restore content" confirmVariant="success" onCancel={()=>setRestore(null)} onConfirm={()=>{if(restore!==null&&!busy){revert.mutate(restore);setRestore(null)}}}/>
  </>
}
