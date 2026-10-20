import {useEffect, useRef, useState} from 'react'
import {InspectionHistory, productionHistory, RecoveryCheck} from '../../api/production'

export default function ProductionHistory({operator,refreshKey=0}:{operator:boolean;refreshKey?:number}) {
  const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('')
  const [query,setQuery]=useState(''),[filter,setFilter]=useState(''),[applied,setApplied]=useState({query:'',state:''})
  const [history,setHistory]=useState<InspectionHistory|null>(null),[checks,setChecks]=useState<Record<string,RecoveryCheck>>({})
  const button='rounded-md border border-[var(--brand-card-border)] px-3 py-2 text-sm disabled:opacity-50'
  const input='min-w-0 w-full rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-content-bg)] px-3 py-2'
  const run=async(fn:()=>Promise<void>)=>{setBusy(true);setError('');try{await fn()}catch(e){setError(e instanceof Error?e.message:'Could not load inspection history.')}finally{setBusy(false)}}
  const load=async(q:string,state:string)=>{const next=await productionHistory.read(0,q,state);setHistory(next);setApplied({query:q,state});setChecks({})}
  const refresh=useRef<()=>void>(()=>{})
  refresh.current=()=>{if(open)void run(()=>load(applied.query,applied.state))}
  useEffect(()=>{refresh.current()},[refreshKey])
  return <section className="space-y-3 border-t border-[var(--brand-card-border)] pt-3">
    <button type="button" className={button} disabled={busy} aria-expanded={open} onClick={()=>{setOpen(!open);if(!open)void run(()=>load(query,filter))}}>Inspection history and Square recovery</button>
    {open&&<div className="space-y-3" aria-busy={busy}>
      <p className="text-sm">Sending is disabled. Checks read Square and can confirm an uncertain addition only when its exact adjustment history matches. An uncertain outcome is never treated as a failed send.</p>
      {busy&&<p role="status">Checking…</p>}{error&&<p role="alert" className="break-words text-red-400">{error}</p>}
      <form className="grid gap-3 sm:grid-cols-2" onSubmit={e=>{e.preventDefault();void run(()=>load(query,filter))}}>
        <label className="text-sm">Search inspections<input aria-label="Search inspections" className={input} maxLength={100} value={query} disabled={busy} placeholder="Product, recipe or job" onChange={e=>setQuery(e.target.value)}/></label>
        <label className="text-sm">Square addition status<select aria-label="Square addition status" className={input} value={filter} disabled={busy} onChange={e=>setFilter(e.target.value)}><option value="">All inspections</option><option value="prepared">Prepared</option><option value="unknown">Unknown outcome</option><option value="confirmed">Confirmed</option></select></label>
        <button className={button} disabled={busy} type="submit">Search or reload history</button>
      </form>
      {history?.receipts.length===0&&<p>No inspections match this search.</p>}
      {history?.receipts.map(r=>{
        const addition=history.additions.find(a=>a.receipt_id===r.id),check=addition?checks[addition.id]:undefined,status=check?.state??addition?.state
        return <article key={r.id} className="space-y-2 rounded-md border border-[var(--brand-card-border)] p-3 text-sm break-words">
          <h3 className="font-medium">{r.recipe_name} · job {r.job_id}</h3><p>{new Date(r.occurred_at).toLocaleString()}</p>
          {r.outputs.map(o=><p key={`${o.product_id}-${o.kind}`}>{o.product_name} ({o.kind}): {o.accepted} accepted, {o.rejected} rejected</p>)}
          <p>Receipt: <span className="font-mono break-all">{r.id}</span></p>
          {!addition?<p>{r.stock_status==='no_sellable_output'?'No sellable output; no Square addition.':'No linked addition found. Retain this receipt for review.'}</p>:<>
            <p>Square: {status==='unknown'?'Unknown outcome':status==='confirmed'?'Confirmed':'Prepared, not sent'} · attempts {check?.attempts??addition.attempts}</p>
            {status==='unknown'&&<p>Do not recreate or resend this addition. Review the exact Square history first.</p>}
            {check&&<p role="status">{check.message}</p>}
            {operator&&status!=='confirmed'&&<button className={button} disabled={busy} type="button" onClick={()=>void run(async()=>{const answer=await productionHistory.check(addition.id);setChecks(old=>({...old,[addition.id]:answer}))})}>{status==='unknown'?'Check Square history':'Check saved destination'}</button>}
          </>}
        </article>
      })}
      {history?.next_offset!=null&&<button className={button} disabled={busy} type="button" onClick={()=>void run(async()=>{const page=await productionHistory.read(history.next_offset!,applied.query,applied.state);setHistory(old=>old?{receipts:[...old.receipts,...page.receipts],additions:[...old.additions,...page.additions],next_offset:page.next_offset}:page)})}>Load older inspections</button>}
    </div>}
  </section>
}
