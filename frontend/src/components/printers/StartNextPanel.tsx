import { useRef, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { jobCompletion } from '../../api/jobCompletion'
import { canDo, getCurrentUser } from '../../permissions'
import { isKobraS1 } from '../../utils/printerMapping'

/** The next job for a printer, and the bed-clear confirmation that starts it. */
export default function StartNextPanel({ printerId, active = true, children }: { printerId: number; active?: boolean; children?: ReactNode }) {
  // An admin session is the owner attesting in the app; anyone else relays the owner's word. The actor is recorded separately.
  const ownerInApp=getCurrentUser()?.role==='admin'
  const [confirmed,setConfirmed]=useState<string|null>(null)
  const [busy,setBusy]=useState(false),[notice,setNotice]=useState(''),[error,setError]=useState('')
  const request=useRef<{id:string;fingerprint:string}|null>(null)
  const {data:next,refetch}=useQuery({queryKey:['next-suggestion',printerId],queryFn:()=>jobCompletion.suggestion(printerId),enabled:active})
  const start=async()=>{
    if(!next?.job_id||!next.fingerprint||confirmed!==next.fingerprint)return
    if(!request.current||request.current.fingerprint!==next.fingerprint)request.current={id:crypto.randomUUID(),fingerprint:next.fingerprint}
    setBusy(true);setError('')
    try{
      const result=await jobCompletion.start(printerId,{suggested_job_id:next.job_id,suggestion_fingerprint:next.fingerprint,request_id:request.current.id,bed_clear_confirmed:true,attestation:ownerInApp?'owner_in_app':'owner_confirmed_to_operator'})
      if(result.success){setNotice(result.state==='dispatched'?'Next job dispatched.':'Start admitted; check dispatch status.');setConfirmed(null)}
      else {setError(result.message||'Start refused. Review the printer and queue.');if(result.state==='refused')request.current=null}
      await refetch()
    }catch(e){setError(e instanceof Error?e.message:'Could not start the next job.');setConfirmed(null);await refetch()}
    finally{setBusy(false)}
  }
  return <>
      {next&&<p>Next: {next.job_id?`job ${next.job_id} · `:''}{next.reason} · {next.filament.replace(/_/g,' ')}</p>}
      {next?.load?.map((v,i)=><p key={i}>Load {v.material} {v.color} in slot {v.slot}</p>)}
    {children}
      {canDo('jobs.start')&&next?.job_id&&<div className="space-y-2">
        <label className="flex gap-2"><input type="checkbox" checked={!!confirmed&&confirmed===next.fingerprint} disabled={busy} onChange={e=>setConfirmed(e.target.checked?next.fingerprint:null)}/>{ownerInApp?'I checked the printer and cleared the bed.':'The owner confirmed to me that the printer is checked and the bed is clear.'}</label>
        <button type="button" disabled={!confirmed||confirmed!==next.fingerprint||busy||!next.fingerprint} onClick={()=>void start()}>Bed clear, start next</button>
      </div>}
      {busy&&<p role="status">Starting…</p>}{notice&&<p role="status">{notice}</p>}{error&&<p role="alert">{error}</p>}
  </>
}

const IDLE_STATES = ['STANDBY', 'COMPLETE', 'CANCELLED']

/** Start next is an S1's only way to start, so its first job and one after a manual print start here. */
export function IdleS1StartNext({ printer }: { printer: { id: number; api_type?: string | null; model?: string | null; machine_type?: string | null; gcode_state?: string | null } }) {
  if (!isKobraS1(printer) || !IDLE_STATES.includes(String(printer.gcode_state || '').toUpperCase())) return null
  return <section aria-label="Start next job" className="mb-6 space-y-2 text-sm"><StartNextPanel printerId={printer.id} /></section>
}
