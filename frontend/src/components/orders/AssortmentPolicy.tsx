import {useState} from 'react'
import {assortment,AssortmentState,AssortmentMode,AssortmentPolicy,ReplenishmentPreview} from '../../api/assortment'
import {getCurrentUser} from '../../permissions'
import { parseServerDate } from '../../utils/shared'

type Numbers={reorder_point:string;target:string;minimum_batch:string}
const EMPTY:Numbers={reorder_point:'',target:'',minimum_batch:''}
const drafted=(policy?:AssortmentPolicy|null):Numbers=>policy?{reorder_point:String(policy.reorder_point),target:String(policy.target),minimum_batch:String(policy.minimum_batch)}:EMPTY
const whole=(value:string,least:number)=>/^\d{1,7}$/.test(value)&&Number(value)>=least?Number(value):null

export default function AssortmentPolicyPanel(){
 const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('')
 const [state,setState]=useState<AssortmentState|null>(null),[productId,setProductId]=useState('')
 const [mode,setMode]=useState<AssortmentMode|''>(''),[draft,setDraft]=useState<Numbers>(EMPTY),[preview,setPreview]=useState<ReplenishmentPreview|null>(null)
 const role=getCurrentUser()?.role,admin=role==='admin',operator=admin||role==='operator'
 const selected=state?.products.find(p=>String(p.id)===productId),saved=drafted(selected?.policy)
 const dirty=mode!==(selected?.policy?.assortment??'')||draft.reorder_point!==saved.reorder_point||draft.target!==saved.target||draft.minimum_batch!==saved.minimum_batch
 const point=whole(draft.reorder_point,0),target=whole(draft.target,0),batch=whole(draft.minimum_batch,1)
 const problem=point===null||target===null||batch===null?'Enter whole numbers in all three fields.':target<point?'“Print up to” cannot be lower than the level that asks for more.':''
 const number=(field:keyof Numbers,label:string,help:string,least:number)=><label className="block text-sm">{label}<input aria-label={label} className={input} type="number" inputMode="numeric" min={least} step={1} required disabled={busy} value={draft[field]} onChange={e=>{setDraft({...draft,[field]:e.target.value});setPreview(null)}}/><span className="block text-xs opacity-80">{help}</span></label>
 const button='rounded-md border border-[var(--brand-card-border)] px-3 py-2 text-sm disabled:opacity-50'
 const input='w-full min-w-0 rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-content-bg)] px-3 py-2'
 const run=async(fn:()=>Promise<void>)=>{setBusy(true);setError('');setNotice('');try{await fn()}catch(e){setError(e instanceof Error?e.message:'Could not check assortment.')}finally{setBusy(false)}}
 const load=async()=>{const saved=await assortment.read();setState(saved);const product=saved.products.find(p=>String(p.id)===productId);setMode(product?.policy?.assortment??'');setDraft(drafted(product?.policy));setPreview(null)}
 return <section className="mb-6 rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-card-bg)] p-4">
  <button type="button" className="font-medium text-[var(--brand-primary)]" disabled={busy} aria-expanded={open} onClick={()=>{setOpen(!open);if(!open)void run(load)}}>Show assortment and replenishment</button>
  {open&&<div className="mt-3 space-y-3" aria-busy={busy}>
   <p className="text-sm">Choose what belongs in the active assortment and when each product should be printed again. Inactive products retain their stock and files. Trials stay deliberate. Nothing here starts a job or changes Square stock.</p>
   {busy&&<p role="status">Checking…</p>}{error&&<p role="alert" className="text-red-400 break-words">{error}</p>}{notice&&<p role="status">{notice}</p>}
   <button type="button" className={button} disabled={busy} onClick={()=>void run(load)}>Reload assortment</button>
   {state?.truncated&&<p>Only the first 1,000 owned products are shown. Review the catalog before expanding the assortment.</p>}
   <label className="block text-sm">Assortment product<select aria-label="Assortment product" className={input} disabled={busy} value={productId} onChange={e=>{const p=state?.products.find(p=>String(p.id)===e.target.value);setProductId(e.target.value);setMode(p?.policy?.assortment??'');setDraft(drafted(p?.policy));setPreview(null);setNotice('')}}><option value="">Choose an owned product</option>{state?.products.map(p=><option key={p.id} value={p.id}>{p.name} · {p.policy?.assortment??'Not configured'}</option>)}</select></label>
   {selected&&<>
    <p className="text-sm">{selected.policy?`Saved: ${selected.policy.assortment}. Print more when stock falls below ${selected.policy.reorder_point}, up to ${selected.policy.target}; smallest batch ${selected.policy.minimum_batch} main pieces per qualified plate.`:'No routine stock policy assumed. Choose a status and the three numbers, then save.'}</p>
    {admin&&<form className="space-y-3" onSubmit={e=>{e.preventDefault();if(!mode||problem||point===null||target===null||batch===null)return;void run(async()=>{const saved=await assortment.save(Number(productId),{revision:state!.revision,assortment:mode,reorder_point:point,target,minimum_batch:batch});setState(saved);setDraft(drafted(saved.products.find(p=>String(p.id)===productId)?.policy));setPreview(null);setNotice('Assortment policy saved. No stock or jobs changed.')})}}>
     <label className="block text-sm">Show assortment status<select aria-label="Show assortment status" value={mode} className={input} required disabled={busy} onChange={e=>{setMode(e.target.value as AssortmentMode);setPreview(null)}}><option value="">Choose status</option><option value="active">Active: eligible for routine suggestions</option><option value="trial">Trial: deliberate limited run</option><option value="inactive">Inactive: keep stock and files</option></select></label>
     {number('reorder_point','Print more when stock falls below','The reorder point. A count at or above it asks for nothing.',0)}
     {number('target','Print up to','The stock level to refill to. Use the same number as above to print one batch at a time.',0)}
     {number('minimum_batch','Smallest batch worth printing','The fewest main pieces a plate must make to be suggested or qualified for this product. Flush outputs are separate.',1)}
     {(draft.reorder_point||draft.target||draft.minimum_batch)&&problem&&<p role="alert" className="text-sm text-red-400">{problem}</p>}
     <button className={button} type="submit" disabled={busy||!mode||!!problem||!dirty}>Save assortment policy</button>
    </form>}
    {operator&&<button type="button" className={button} disabled={busy||dirty} onClick={()=>void run(async()=>{setPreview(null);setPreview(await assortment.preview(Number(productId)))})}>Check replenishment</button>}
    {dirty&&<p className="text-sm">Save or reload the policy before checking replenishment.</p>}
   </>}
   {preview&&<article className="space-y-2 rounded-md border border-[var(--brand-card-border)] p-3 text-sm break-words">
    <p role="status">{preview.message}</p>
    {preview.stock&&<p>Square recorded quantity: {preview.stock.quantity??'Unknown'}. This is not a physical stock count.</p>}
    {preview.retrieved_at&&<p>Checked {parseServerDate(preview.retrieved_at).toLocaleString()}. Counts and queued work can change after this check.</p>}
    {preview.recipes.map(r=><div key={r.id} className="space-y-1 border-t border-[var(--brand-card-border)] pt-2"><h3 className="font-medium">{r.name}: one plate produces {r.main_yield} main pieces</h3><p>Printer {r.printer_id} · {r.materials}</p>{r.outputs.map((o,i)=><p key={i}>{o.expected} {o.product_name} ({o.kind})</p>)}</div>)}
    {preview.recipes.length>0&&<p>Advisory only. Protect paid-order and wholesale promises. Loaded materials, maintenance, physical stock and a freshly cleared plate still need checking before a start.</p>}
   </article>}
  </div>}
 </section>
}
