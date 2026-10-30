import { displayName } from '../../utils/displayName'
import MaintenanceBanner from '../printers/MaintenanceBanner'
import StartNextPanel from '../printers/StartNextPanel'
import { useState } from 'react'
import type { CompletionSummary as Summary } from '../../api/jobCompletion'
import { canDo } from '../../permissions'
import ProductionBatches from '../orders/ProductionBatches'

export default function CompletionSummary({summary}:{summary:Summary}) {
  const [expanded,setExpanded]=useState(false),[counting,setCounting]=useState(false)
  return <section className="mt-3 space-y-3 text-sm">
    <button type="button" aria-expanded={expanded} onClick={()=>setExpanded(v=>!v)}>Completion summary · job {summary.job_id}</button>
    {expanded&&<div className="space-y-3 break-words">
      <p>{summary.printer} · {displayName(summary)} · {summary.actual_print_seconds===null?'Print time unknown':`${Math.round(summary.actual_print_seconds/60)} min`}</p>
      <p>{summary.recipe_state==='matched'?summary.product.join(', '):summary.recipe_state}</p>
      <ul>{summary.expected_outputs.map(o=><li key={`${o.product_id}-${o.kind}`}>{o.expected} {o.product_name} ({o.kind})</li>)}</ul>
      {summary.maintenance && summary.maintenance.state !== 'ok' && <p role="status">Maintenance {summary.maintenance.state}: {summary.maintenance.note}</p>}
      <p>Health: {summary.health}</p>
      <ul>{summary.health_events.map((e,i)=><li key={i}>{e.kind}{e.code?` · ${e.code}`:''}</li>)}</ul>
      <p>Clear the plate, count, bin, and record the inspection.</p>
      {!summary.inspection_available&&<p>inspection unavailable: {summary.inspection_unavailable_reason}</p>}
      {summary.square_projection.length?summary.square_projection.map(p=><p key={p.product_id}>{p.quantity===null?'Square count unknown':`Square → ${p.quantity} if all good`}</p>):<p>Square count unknown</p>}
      <a href={`/products?inspect=${summary.job_id}`}>Open inspection</a>
      {summary.inspection_available&&canDo('products.inspections')&&<button type="button" aria-expanded={counting} onClick={()=>setCounting(v=>!v)}>Record count</button>}
      {counting&&<ProductionBatches products={[]} inspectJobId={summary.job_id}/>}
      <p>{summary.bed_line}</p>
      {summary.printer_id && <StartNextPanel printerId={summary.printer_id} active={expanded}><MaintenanceBanner printerId={summary.printer_id} /></StartNextPanel>}
    </div>}
  </section>
}
