import {useState} from 'react'
import {showFulfillment,ShowFacts,ShowIntake,ShippingAddress} from '../../api/showFulfillment'

const emptyAddress:ShippingAddress={recipient:'',line1:'',line2:'',city:'',region:'',postal_code:'',country:'US'}
const blank:ShowIntake={revision:null,address:emptyAddress,address_confirmed:false,source:'',dispatch_by:'',dispatch_timezone:'America/New_York',currency:'USD',amount_received_minor:0,payment_method:'square_external',payment_reference:'',payment_received_at:null,payment_confirmed:false}
const labels:Record<string,string>={not_enrolled:'Not enrolled',quote_review:'Review the existing order quote and item identities first',payment_unconfirmed:'Payment not confirmed',partial_payment:'Partial payment recorded',address_unconfirmed:'Confirm the shipping address',paid_committed:'Manually confirmed paid commitment',quote_changed:'Order quote or contents changed; review and reconfirm',cancelled_payment_review:'Cancelled: received payment needs refund review',cancelled:'Cancelled',dispatched:'Physical parcel handoff recorded',dispatch_review:'Handoff retained; underlying order needs review'}
export function amountToCents(value:string):number|null {
 if(!/^\d{1,7}(\.\d{1,2})?$/.test(value))return null
 const [whole,fraction='']=value.split('.');const result=Number(whole)*100+Number(fraction.padEnd(2,'0'))
 return result<=100000000?result:null
}
function localTime(value:string|null){if(!value)return '';const d=new Date(value);if(!Number.isFinite(d.getTime()))return '';d.setMinutes(d.getMinutes()-d.getTimezoneOffset());return d.toISOString().slice(0,16)}
function iso(value:string){return new Date(value).toISOString()}

export default function ShowFulfillment({orderId,canEdit,canShip,onUpdated}:{orderId:number;canEdit:boolean;canShip:boolean;onUpdated?:(id:number)=>void}){
 const [dirty,setDirty]=useState(false)
 const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('')
 const [facts,setFacts]=useState<ShowFacts|null>(null),[form,setForm]=useState<ShowIntake>(blank),[amount,setAmount]=useState('0.00'),[paidAt,setPaidAt]=useState('')
 const [carrier,setCarrier]=useState(''),[tracking,setTracking]=useState(''),[handedAt,setHandedAt]=useState(''),[handed,setHanded]=useState(false)
 const button='rounded-md border border-[var(--brand-card-border)] px-3 py-2 text-sm disabled:opacity-50'
 const input='w-full min-w-0 rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-content-bg)] px-3 py-2'
 const run=async(fn:()=>Promise<void>)=>{setBusy(true);setError('');setNotice('');try{await fn()}catch(e){setError(e instanceof Error?e.message:'Could not update show-order details.')}finally{setBusy(false)}}
 const receive=(saved:ShowFacts)=>{setDirty(false);setFacts(saved);const d=saved.details;setForm(d?{...d,revision:saved.revision,address_confirmed:false,payment_confirmed:false}:blank);setAmount(((d?.amount_received_minor??0)/100).toFixed(2));setPaidAt(localTime(d?.payment_received_at??null));setHanded(false)}
 const load=async()=>receive(await showFulfillment.read(orderId))
 const field=(key:keyof ShowIntake,value:unknown)=>{setDirty(true);setForm(old=>({...old,[key]:value,payment_confirmed:false}))}
 const address=(key:keyof ShippingAddress,value:string)=>{setDirty(true);setForm(old=>({...old,address:{...old.address,[key]:value},address_confirmed:false,payment_confirmed:false}))}
 return <section className="space-y-3 border-t border-[var(--brand-card-border)] pt-3">
  <button type="button" className={button} disabled={busy} aria-expanded={open} onClick={()=>{setOpen(!open);if(!open)void run(load)}}>Show-order shipping and payment</button>
  {open&&<div className="space-y-3" aria-busy={busy}>
   <p className="text-sm">Record facts from payment taken elsewhere. This does not charge or refund a customer, verify a provider receipt, buy a label or send a message. Do not enter card or bank details.</p>
   {busy&&<p role="status">Saving or checking…</p>}{error&&<p role="alert" className="break-words text-red-400">{error}</p>}{notice&&<p role="status">{notice}</p>}
   <button type="button" className={button} disabled={busy} onClick={()=>void run(load)}>Reload saved commitment</button>
   {facts&&<div className="space-y-1 text-sm"><p>Status: {labels[facts.state]??facts.state}</p><p>Quoted order total including shipping: {facts.quoted_total_minor==null?'Review order quote':`$${(facts.quoted_total_minor/100).toFixed(2)} USD`}</p><p>Remaining production: {facts.production_remaining} pieces</p>{facts.details&&<p>Promised dispatch by {facts.details.dispatch_by}. {facts.overdue?'Promise date has passed; review the commitment.':'This date is not a capacity guarantee.'}</p>}{facts.dispatch&&<p>Carrier handoff: {facts.dispatch.carrier} · {facts.dispatch.tracking_number} · {new Date(facts.dispatch.handed_at).toLocaleString()}</p>}</div>}
   {!!facts?.payment_history?.length&&<details className="text-sm"><summary>Retained receipt history</summary>{facts.payment_history.map((h,i)=><p key={i}>${(h.amount_received_minor/100).toFixed(2)} USD · {h.payment_method} · {h.payment_reference} · retained {new Date(h.recorded_at).toLocaleString()}</p>)}</details>}
   {facts&&facts.quoted_total_minor!=null&&canEdit&&!facts.dispatch&&facts.order_status!=='cancelled'&&facts.order_status!=='shipped'&&<form className="space-y-3" onSubmit={e=>{e.preventDefault();const minor=amountToCents(amount);if(minor==null){setError('Enter a nonnegative USD amount with at most two decimal places.');return}void run(async()=>{const saved=await showFulfillment.save(orderId,{...form,revision:facts.revision,amount_received_minor:minor,payment_received_at:paidAt===localTime(facts.details?.payment_received_at??null)?(facts.details?.payment_received_at??null):(paidAt?iso(paidAt):null)});receive(saved);setNotice('Show-order facts saved. No payment taken or message sent.');onUpdated?.(orderId)})}}>
    <label className="block text-sm">Show or source<input aria-label="Show or source" className={input} maxLength={160} required disabled={busy} value={form.source} onChange={e=>field('source',e.target.value)}/></label>
    <div className="grid gap-3 sm:grid-cols-2">{(['recipient','line1','line2','city','region','postal_code'] as const).map(key=><label className="block text-sm" key={key}>{({recipient:'Recipient',line1:'Address line 1',line2:'Address line 2',city:'City',region:'State',postal_code:'ZIP code'})[key]}<input aria-label={`Shipping ${key}`} className={input} maxLength={key==='postal_code'?30:key==='recipient'?160:key==='city'||key==='region'?100:200} required={key!=='line2'} disabled={busy} value={form.address[key]} onChange={e=>address(key,e.target.value)}/></label>)}</div>
    <p className="text-sm">United States shipping only in this intake flow. Address confirmation is manual, not carrier validation.</p>
    <label className="flex gap-2 text-sm"><input type="checkbox" disabled={busy} checked={form.address_confirmed} onChange={e=>{setDirty(true);setForm(old=>({...old,address_confirmed:e.target.checked}))}}/>I confirmed this shipping address with the customer.</label>
    <label className="block text-sm">Promised dispatch date<input aria-label="Promised dispatch date" className={input} type="date" required disabled={busy} value={form.dispatch_by} onChange={e=>field('dispatch_by',e.target.value)}/></label>
    <label className="block text-sm">Promise time zone<select aria-label="Promise time zone" className={input} value={form.dispatch_timezone??'America/New_York'} disabled={busy} onChange={e=>field('dispatch_timezone',e.target.value)}>{['America/New_York','America/Chicago','America/Denver','America/Phoenix','America/Los_Angeles','America/Anchorage','Pacific/Honolulu','UTC'].map(zone=><option key={zone} value={zone}>{zone}</option>)}</select></label>
    <div className="grid gap-3 sm:grid-cols-2"><label className="block text-sm">Amount already received, USD<input aria-label="Amount received USD" inputMode="decimal" className={input} maxLength={12} required disabled={busy} value={amount} onChange={e=>{setAmount(e.target.value);field('payment_confirmed',false)}}/></label>
    <label className="block text-sm">Payment method<select aria-label="Payment method" className={input} disabled={busy} value={form.payment_method} onChange={e=>field('payment_method',e.target.value)}><option value="square_external">Square receipt, entered manually</option><option value="cash">Cash</option><option value="other_external">Other external receipt</option></select></label></div>
    <label className="block text-sm">External receipt reference<input aria-label="External receipt reference" className={input} maxLength={160} disabled={busy} value={form.payment_reference} onChange={e=>field('payment_reference',e.target.value)}/></label>
    <label className="block text-sm">Actual payment time<input aria-label="Actual payment time" type="datetime-local" className={input} disabled={busy} value={paidAt} onChange={e=>{setPaidAt(e.target.value);field('payment_confirmed',false)}}/></label>
    <label className="flex gap-2 text-sm"><input type="checkbox" disabled={busy} checked={form.payment_confirmed} onChange={e=>{setDirty(true);setForm(old=>({...old,payment_confirmed:e.target.checked}))}}/>I checked the receipt and confirm the full quoted amount, including shipping, was received.</label>
    <label className="block text-sm">Receipt correction reason<input aria-label="Receipt correction reason" className={input} maxLength={400} disabled={busy} value={form.correction_note??''} onChange={e=>field('correction_note',e.target.value)}/></label>
    <p className="text-sm">After a quote, item or address correction, review and confirm again. Existing invoices remain available for customer review; nothing is automatically emailed.</p>
    <button type="submit" className={button} disabled={busy||amountToCents(amount)==null}>Save show-order facts</button>
   </form>}
   {dirty&&facts?.can_dispatch&&<p>Save or reload edited details before confirming parcel handoff.</p>}
   {facts?.can_dispatch&&canShip&&!dirty&&<form className="space-y-3 border-t border-[var(--brand-card-border)] pt-3" onSubmit={e=>{e.preventDefault();if(!handed||!facts.revision)return;void run(async()=>{const saved=await showFulfillment.dispatch(orderId,{revision:facts.revision,carrier,tracking_number:tracking,handed_at:iso(handedAt),physically_handed_over:handed});receive(saved);setNotice('Physical parcel handoff recorded. No label purchased or message sent.');onUpdated?.(orderId)})}}>
    <h3 className="font-medium">Confirm actual parcel handoff</h3><p className="text-sm">A purchased label or tracking number alone does not mean shipped.</p>
    <label className="block text-sm">Carrier<input aria-label="Handoff carrier" className={input} required maxLength={80} disabled={busy} value={carrier} onChange={e=>{setCarrier(e.target.value);setHanded(false)}}/></label>
    <label className="block text-sm">Tracking number<input aria-label="Handoff tracking number" className={input} required maxLength={100} disabled={busy} value={tracking} onChange={e=>{setTracking(e.target.value);setHanded(false)}}/></label>
    <label className="block text-sm">Actual handoff time<input aria-label="Actual handoff time" type="datetime-local" className={input} required disabled={busy} value={handedAt} onChange={e=>{setHandedAt(e.target.value);setHanded(false)}}/></label>
    <label className="flex gap-2 text-sm"><input type="checkbox" disabled={busy} checked={handed} onChange={e=>setHanded(e.target.checked)}/>The completed parcel was physically handed to the carrier.</label>
    <button type="submit" className={button} disabled={busy||!handed||!carrier||!tracking||!handedAt}>Record physical handoff</button>
   </form>}
   {facts?.state==='cancelled_payment_review'&&<p className="text-sm">Keep the order and receipt. Decide how to handle the received payment; no refund or printer cancellation was performed.</p>}
  </div>}
 </section>
}
