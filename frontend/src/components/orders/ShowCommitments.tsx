import {useState} from 'react'
import {showFulfillment,ShowCommitments as Commitments} from '../../api/showFulfillment'
export default function ShowCommitments({onOpen}:{onOpen:(id:number)=>void}){
 const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[state,setState]=useState<Commitments|null>(null)
 const load=async(offset=0)=>{setBusy(true);setError('');try{const page=await showFulfillment.commitments(offset);setState(old=>offset&&old?{...page,orders:[...old.orders,...page.orders]}:page)}catch{setError('Could not read show commitments. Reload to check saved orders.')}finally{setBusy(false)}}
 return <section className="mb-4 space-y-3 rounded-md border border-[var(--brand-card-border)] p-4">
  <button type="button" disabled={busy} aria-expanded={open} onClick={()=>{setOpen(!open);if(!open)void load()}}>Paid show commitments</button>
  {open&&<div className="space-y-2 text-sm" aria-busy={busy}><p>Paid customer commitments precede routine stock. Protect promised wholesale deadlines; conflicts require a decision. This view does not certify capacity or change the printer queue.</p>{error&&<p role="alert">{error}</p>}<button type="button" disabled={busy} onClick={()=>void load()}>Reload commitments</button>
   {state?.orders.length===0&&<p>No enrolled commitments on this page of open orders.</p>}
   {state?.orders.map(o=><article key={o.order_id} className="space-y-1 border-t border-[var(--brand-card-border)] pt-2"><button type="button" className="text-[var(--brand-primary)]" onClick={()=>onOpen(o.order_id)}>Order {o.order_number||o.order_id}</button><p>{o.state.replace(/_/g,' ')} · dispatch by {o.dispatch_by} · {o.production_remaining} pieces remaining</p>{o.overdue&&<p>Promised date has passed. Review the commitment.</p>}</article>)}
   {state?.next_offset!=null&&<button type="button" disabled={busy} onClick={()=>void load(state.next_offset!)}>Check older open orders</button>}
  </div>}
 </section>
}
