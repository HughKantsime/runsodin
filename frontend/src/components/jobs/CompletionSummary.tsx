import { useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { CompletionSummary as Summary, jobCompletion } from '../../api/jobCompletion'
import { canDo } from '../../permissions'
import ProductionBatches from '../orders/ProductionBatches'

export default function CompletionSummary({summary}:{summary:Summary}) {
  const [expanded,setExpanded]=useState(false),[counting,setCounting]=useState(false),[confirmed,setConfirmed]=useState<string|null>(null)
  const [busy,setBusy]=useState(false),[notice,setNotice]=useState(''),[error,setError]=useState('')
  const request=useRef<{id:string;fingerprint:string}|null>(null)
  const {data:next,refetch}=useQuery({queryKey:['next-suggestion',summary.printer_id],queryFn:()=>jobCompletion.suggestion(summary.printer_id!),enabled:expanded&&summary.printer_id!==null})
  const start=async()=>{
    if(!summary.printer_id||!next?.job_id||!next.fingerprint||confirmed!==next.fingerprint)return
    if(!request.current||request.current.fingerprint!==next.fingerprint)request.current={id:crypto.randomUUID(),fingerprint:next.fingerprint}
    setBusy(true);setError('')
    try{
      const result=await jobCompletion.start(summary.printer_id,{suggested_job_id:next.job_id,suggestion_fingerprint:next.fingerprint,request_id:request.current.id,bed_clear_confirmed:true,attestation:'owner_in_app'})
      if(result.success){setNotice(result.state==='dispatched'?'Next job dispatched.':'Start admitted; check dispatch status.');setConfirmed(null)}
      else setError(result.message||'Start refused. Review the printer and queue.')
      await refetch()
    }catch(e){setError(e instanceof Error?e.message:'Could not start the next job.');setConfirmed(null);await refetch()}
    finally{setBusy(false)}
  }
  return <section className="mt-3 space-y-3 text-sm">
    <button type="button" aria-expanded={expanded} onClick={()=>setExpanded(v=>!v)}>Completion summary · job {summary.job_id}</button>
    {expanded&&<div className="space-y-3 break-words">
      <p>{summary.printer} · {summary.job_name} · {summary.actual_print_seconds===null?'Print time unknown':`${Math.round(summary.actual_print_seconds/60)} min`}</p>
      <p>{summary.recipe_state==='matched'?summary.product.join(', '):summary.recipe_state}</p>
      <ul>{summary.expected_outputs.map(o=><li key={`${o.product_id}-${o.kind}`}>{o.expected} {o.product_name} ({o.kind})</li>)}</ul>
      <p>Health: {summary.health}</p>
      <ul>{summary.health_events.map((e,i)=><li key={i}>{e.kind}{e.code?` · ${e.code}`:''}</li>)}</ul>
      <p>Clear the plate, count, bin, and record the inspection.</p>
      {!summary.inspection_available&&<p>inspection unavailable: {summary.inspection_unavailable_reason}</p>}
      {summary.square_projection.length?summary.square_projection.map(p=><p key={p.product_id}>{p.quantity===null?'Square count unknown':`Square → ${p.quantity} if all good`}</p>):<p>Square count unknown</p>}
      <a href={`/products?inspect=${summary.job_id}`}>Open inspection</a>
      {summary.inspection_available&&canDo('products.inspections')&&<button type="button" aria-expanded={counting} onClick={()=>setCounting(v=>!v)}>Record count</button>}
      {counting&&<ProductionBatches products={[]} inspectJobId={summary.job_id}/>}
      <p>{summary.bed_line}</p>
      {next&&<p>Next: {next.job_id?`job ${next.job_id} · `:''}{next.reason} · {next.filament.replace(/_/g,' ')}</p>}
      {next?.load?.map((v,i)=><p key={i}>Load {v.material} {v.color} in slot {v.slot}</p>)}
      {canDo('jobs.start')&&next?.job_id&&<div className="space-y-2">
        <label className="flex gap-2"><input type="checkbox" checked={!!confirmed&&confirmed===next.fingerprint} disabled={busy} onChange={e=>setConfirmed(e.target.checked?next.fingerprint:null)}/>I checked the printer and cleared the bed.</label>
        <button type="button" disabled={!confirmed||confirmed!==next.fingerprint||busy||!next.fingerprint} onClick={()=>void start()}>Bed clear, start next</button>
      </div>}
      {busy&&<p role="status">Starting…</p>}{notice&&<p role="status">{notice}</p>}{error&&<p role="alert">{error}</p>}
    </div>}
  </section>
}
