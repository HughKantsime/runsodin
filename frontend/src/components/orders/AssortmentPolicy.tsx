import {useState} from 'react'
import {assortment,AssortmentState,AssortmentMode,ProductKind,ReplenishmentPreview} from '../../api/assortment'
import {getCurrentUser} from '../../permissions'

export default function AssortmentPolicyPanel(){
 const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('')
 const [state,setState]=useState<AssortmentState|null>(null),[productId,setProductId]=useState('')
 const [mode,setMode]=useState<AssortmentMode|''>(''),[kind,setKind]=useState<ProductKind|''>(''),[preview,setPreview]=useState<ReplenishmentPreview|null>(null)
 const role=getCurrentUser()?.role,admin=role==='admin',operator=admin||role==='operator'
 const selected=state?.products.find(p=>String(p.id)===productId),dirty=mode!==(selected?.policy?.assortment??'')||kind!==(selected?.policy?.kind??'')
 const button='rounded-md border border-[var(--brand-card-border)] px-3 py-2 text-sm disabled:opacity-50'
 const input='w-full min-w-0 rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-content-bg)] px-3 py-2'
 const run=async(fn:()=>Promise<void>)=>{setBusy(true);setError('');setNotice('');try{await fn()}catch(e){setError(e instanceof Error?e.message:'Could not check assortment.')}finally{setBusy(false)}}
 const load=async()=>{const saved=await assortment.read();setState(saved);const product=saved.products.find(p=>String(p.id)===productId);setMode(product?.policy?.assortment??'');setKind(product?.policy?.kind??'');setPreview(null)}
 return <section className="mb-6 rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-card-bg)] p-4">
  <button type="button" className="font-medium text-[var(--brand-primary)]" disabled={busy} aria-expanded={open} onClick={()=>{setOpen(!open);if(!open)void run(load)}}>Show assortment and replenishment</button>
  {open&&<div className="mt-3 space-y-3" aria-busy={busy}>
   <p className="text-sm">Choose what belongs in the active show assortment. Inactive products retain their stock and files. Trials stay deliberate. Nothing here starts a job or changes Square stock.</p>
   {busy&&<p role="status">Checking…</p>}{error&&<p role="alert" className="text-red-400 break-words">{error}</p>}{notice&&<p role="status">{notice}</p>}
   <button type="button" className={button} disabled={busy} onClick={()=>void run(load)}>Reload assortment</button>
   {state?.truncated&&<p>Only the first 1,000 owned products are shown. Review the catalog before expanding the assortment.</p>}
   <label className="block text-sm">Assortment product<select aria-label="Assortment product" className={input} disabled={busy} value={productId} onChange={e=>{const p=state?.products.find(p=>String(p.id)===e.target.value);setProductId(e.target.value);setMode(p?.policy?.assortment??'');setKind(p?.policy?.kind??'');setPreview(null);setNotice('')}}><option value="">Choose an owned product</option>{state?.products.map(p=><option key={p.id} value={p.id}>{p.name} · {p.policy?.assortment??'Not configured'}</option>)}</select></label>
   {selected&&<>
    <p className="text-sm">{selected.policy?`Saved: ${selected.policy.assortment}. Trigger below ${selected.rules.trigger_below} recorded pieces when active; minimum ${selected.rules.minimum_main_batch} main pieces per qualified plate.`:'No routine stock policy assumed. Choose and save a classification when ready.'}</p>
    {admin&&<form className="space-y-3" onSubmit={e=>{e.preventDefault();if(!mode||!kind)return;void run(async()=>{const saved=await assortment.save(Number(productId),{revision:state!.revision,assortment:mode,kind});setState(saved);setPreview(null);setNotice('Assortment policy saved. No stock or jobs changed.')})}}>
     <label className="block text-sm">Show assortment status<select aria-label="Show assortment status" value={mode} className={input} required disabled={busy} onChange={e=>{setMode(e.target.value as AssortmentMode);setPreview(null)}}><option value="">Choose status</option><option value="active">Active: eligible for routine suggestions</option><option value="trial">Trial: deliberate limited run</option><option value="inactive">Inactive: keep stock and files</option></select></label>
     <label className="block text-sm">Production category<select aria-label="Production category" value={kind} className={input} required disabled={busy} onChange={e=>{setKind(e.target.value as ProductKind);setPreview(null)}}><option value="">Choose category</option><option value="multicolor_character">Multicolor character</option><option value="dragon_jumbo">Jumbo dragon</option><option value="dragon_half">Half-size dragon</option><option value="dragon_other">Other dragon</option><option value="other">Other finished product</option></select></label>
     <p className="text-sm">Stock triggers are five per active item, or fifteen per jumbo/half-size dragon design. They are not refill targets. Multicolor characters have a separate five-piece minimum main batch, including trials. Small flush outputs are separate.</p>
     <button className={button} type="submit" disabled={busy||!mode||!kind||!dirty}>Save assortment policy</button>
    </form>}
    {operator&&<button type="button" className={button} disabled={busy||dirty} onClick={()=>void run(async()=>{setPreview(null);setPreview(await assortment.preview(Number(productId)))})}>Check replenishment</button>}
    {dirty&&<p className="text-sm">Save or reload the policy before checking replenishment.</p>}
   </>}
   {preview&&<article className="space-y-2 rounded-md border border-[var(--brand-card-border)] p-3 text-sm break-words">
    <p role="status">{preview.message}</p>
    {preview.stock&&<p>Square recorded quantity: {preview.stock.quantity??'Unknown'}. This is not a physical stock count.</p>}
    {preview.retrieved_at&&<p>Checked {new Date(preview.retrieved_at).toLocaleString()}. Counts and queued work can change after this check.</p>}
    {preview.recipes.map(r=><div key={r.id} className="space-y-1 border-t border-[var(--brand-card-border)] pt-2"><h3 className="font-medium">{r.name}: one plate produces {r.main_yield} main pieces</h3><p>Printer {r.printer_id} · {r.materials}</p>{r.outputs.map((o,i)=><p key={i}>{o.expected} {o.product_name} ({o.kind})</p>)}</div>)}
    {preview.recipes.length>0&&<p>Advisory only. Protect paid-order and wholesale promises. Loaded materials, maintenance, physical stock and a freshly cleared plate still need checking before a start.</p>}
   </article>}
  </div>}
 </section>
}
